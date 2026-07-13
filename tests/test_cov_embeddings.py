"""Coverage tests for src/embeddings.py — pure helpers, backend resolution,
model name resolution, dead-host cooldown, and the persistent (L2) cache.

Kept independent of tests/test_embeddings.py to avoid parallel-run conflicts.
"""
from __future__ import annotations

import json
import math
import time
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src import embeddings as emb


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Hermetic per-test state: L2 cache off by default, host tables cleared."""
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    monkeypatch.delenv("ENGRAM_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAM_EMBED_MODEL", raising=False)
    monkeypatch.delenv("ENGRAM_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("ENGRAM_EMBED_MAX_CHARS", raising=False)
    emb._dead_hosts.clear()
    emb._host_fails.clear()
    emb._last_embed_failure_reason = None
    emb.clear_embedding_cache()
    yield
    emb._dead_hosts.clear()
    emb._host_fails.clear()
    emb._last_embed_failure_reason = None
    emb.clear_embedding_cache()


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


# --------------------------------------------------------------------------
# l2_normalize
# --------------------------------------------------------------------------
def test_l2_normalize_produces_unit_vector():
    out = emb.l2_normalize([3.0, 4.0])
    assert out == pytest.approx([0.6, 0.8])
    assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0)


def test_l2_normalize_zero_vector_returned_unchanged():
    zero = [0.0, 0.0, 0.0]
    assert emb.l2_normalize(zero) == zero


# --------------------------------------------------------------------------
# resolve_embed_backend / endpoint helpers
# --------------------------------------------------------------------------
def test_resolve_backend_ollama_default(monkeypatch):
    kind, base = emb.resolve_embed_backend()
    assert kind == "ollama"
    assert base == config.ollama_host()


def test_resolve_backend_disabled(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "DISABLED")
    assert emb.resolve_embed_backend() == ("disabled", "")


def test_resolve_backend_openai_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "https://api.example.com/v1/")
    assert emb.resolve_embed_backend() == ("openai", "https://api.example.com/v1")


def test_openai_endpoint_url_forms():
    assert emb._openai_embeddings_endpoint("https://h") == "https://h/v1/embeddings"
    assert emb._openai_embeddings_endpoint("https://h/v1") == "https://h/v1/embeddings"
    assert (
        emb._openai_embeddings_endpoint("https://h/v1/embeddings")
        == "https://h/v1/embeddings"
    )


# --------------------------------------------------------------------------
# availability / degradation reason
# --------------------------------------------------------------------------
def test_host_available_when_ollama_and_not_cooldown():
    assert emb.is_embedding_host_available() is True


def test_host_unavailable_when_disabled(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    assert emb.is_embedding_host_available() is False


def test_host_unavailable_when_in_cooldown():
    _, base = emb.resolve_embed_backend()
    emb._dead_hosts[emb._host_key(base)] = time.time() + 100
    assert emb.is_embedding_host_available() is False


def test_degradation_reason_disabled(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    assert emb.get_embedding_degradation_reason() == (
        "embeddings_disabled (ENGRAM_EMBED_URL=disabled)"
    )


def test_degradation_reason_cooldown_uses_last_failure():
    _, base = emb.resolve_embed_backend()
    emb._dead_hosts[emb._host_key(base)] = time.time() + 100
    emb._last_embed_failure_reason = "ollama_host_cooldown (20s)"
    assert emb.get_embedding_degradation_reason() == "ollama_host_cooldown (20s)"


def test_degradation_reason_none_when_healthy():
    assert emb.get_embedding_degradation_reason() is None


# --------------------------------------------------------------------------
# model-name resolution
# --------------------------------------------------------------------------
def test_resolve_model_default_when_unset():
    assert emb.resolve_embedding_model_name() == emb._DEFAULT_MODEL


def test_resolve_model_primary_wins(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_MODEL", "mxbai-embed-large")
    assert emb.resolve_embedding_model_name() == "mxbai-embed-large"


def test_resolve_model_primary_and_conflicting_legacy_warns(monkeypatch, caplog):
    monkeypatch.setenv("ENGRAM_EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setenv("ENGRAM_EMBEDDING_MODEL", "bge-large")
    with caplog.at_level("WARNING"):
        assert emb.resolve_embedding_model_name() == "nomic-embed-text"
    assert any("both set" in r.message for r in caplog.records)


def test_resolve_model_legacy_only_is_deprecated(monkeypatch, caplog):
    monkeypatch.setenv("ENGRAM_EMBEDDING_MODEL", "bge-large")
    with caplog.at_level("WARNING"):
        assert emb.resolve_embedding_model_name() == "bge-large"
    assert any("deprecated" in r.message for r in caplog.records)


def test_get_model_warns_on_unknown(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_MODEL", "totally-made-up-model")
    with pytest.warns(UserWarning, match="not in the known-models list"):
        assert emb._get_model() == "totally-made-up-model"


def test_get_embedding_model_returns_active(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_MODEL", "snowflake-arctic-embed2")
    assert emb.get_embedding_model() == "snowflake-arctic-embed2"


# --------------------------------------------------------------------------
# dimension helpers
# --------------------------------------------------------------------------
def test_expected_dimensions_known_and_unknown():
    assert emb.expected_dimensions_for_model("nomic-embed-text") == 768
    assert emb.expected_dimensions_for_model("no-such-model") is None


def test_matches_schema_ok_for_matching_length():
    ok, err = emb.embedding_matches_vec_schema([0.0] * 768, "nomic-embed-text")
    assert ok is True and err is None


def test_matches_schema_rejects_known_model_dim_mismatch():
    ok, err = emb.embedding_matches_vec_schema([0.0] * 768, "mxbai-embed-large")
    assert ok is False
    assert "1024-dim" in err and "migrate-embeddings" in err


def test_matches_schema_rejects_length_mismatch_unknown_model():
    ok, err = emb.embedding_matches_vec_schema([0.0] * 10, "custom", expected_dim=768)
    assert ok is False
    assert "embedding length 10 != vec_memory dimension 768" in err
    assert "custom" in err


# --------------------------------------------------------------------------
# dead-host cooldown state machine
# --------------------------------------------------------------------------
def test_mark_host_failure_trips_cooldown_after_threshold():
    base = "http://localhost:11434"
    key = emb._host_key(base)
    emb._mark_host_failure(base)
    assert key not in emb._dead_hosts  # one failure is below threshold
    emb._mark_host_failure(base)
    assert key in emb._dead_hosts
    assert emb._is_host_in_cooldown(base) is True
    assert "cooldown" in emb._last_embed_failure_reason


def test_mark_host_success_clears_failures():
    base = "http://localhost:11434"
    emb._mark_host_failure(base)
    emb._mark_host_failure(base)
    emb._mark_host_success(base)
    assert emb._host_key(base) not in emb._dead_hosts
    assert emb._host_key(base) not in emb._host_fails
    assert emb._is_host_in_cooldown(base) is False


def test_is_host_in_cooldown_expires():
    base = "http://localhost:11434"
    emb._dead_hosts[emb._host_key(base)] = time.time() - 1  # already expired
    assert emb._is_host_in_cooldown(base) is False
    assert emb._host_key(base) not in emb._dead_hosts  # popped on expiry


# --------------------------------------------------------------------------
# embed_text paths
# --------------------------------------------------------------------------
def test_embed_text_empty_returns_none_without_http():
    with patch("urllib.request.urlopen") as m:
        assert emb.embed_text("") is None
    m.assert_not_called()


def test_embed_text_disabled_sets_reason(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    assert emb.embed_text("hi") is None
    assert emb._last_embed_failure_reason == "embeddings_disabled"


def test_embed_text_success_normalizes_and_caches():
    raw = [3.0] * 768
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": raw})) as m:
        first = emb.embed_text("hello")
        second = emb.embed_text("hello")
    assert first == _unit(raw)
    assert second == first
    assert m.call_count == 1  # second served from LRU


def test_embed_text_truncates_to_max_chars(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_MAX_CHARS", "50")
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": [0.1] * 768})) as m:
        emb.embed_text("z" * 9000)
    body = json.loads(m.call_args[0][0].data.decode())
    assert len(body["prompt"]) == 50


def test_embed_text_cooldown_short_circuits_without_http():
    _, base = emb.resolve_embed_backend()
    emb._dead_hosts[emb._host_key(base)] = time.time() + 100
    with patch("urllib.request.urlopen") as m:
        assert emb.embed_text("cold") is None
    m.assert_not_called()
    assert emb._last_embed_failure_reason == "ollama_host_cooldown"


def test_embed_text_request_failure_marks_reason():
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert emb.embed_text("boom") is None
    assert emb._last_embed_failure_reason == "ollama_request_failed"


def test_embed_text_openai_backend_sends_auth_and_input(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "https://api.example.com/v1")
    monkeypatch.setenv("ENGRAM_EMBED_API_KEY", "sk-xyz")
    raw = [0.5] * 1024
    resp = _mock_response({"data": [{"embedding": raw}]})
    with patch("urllib.request.urlopen", return_value=resp) as m:
        out = emb.embed_text("query", model="text-embedding-3-small")
    assert out == _unit(raw)
    req = m.call_args[0][0]
    assert req.full_url == "https://api.example.com/v1/embeddings"
    assert req.get_header("Authorization") == "Bearer sk-xyz"
    body = json.loads(req.data.decode())
    assert body == {"model": "text-embedding-3-small", "input": "query"}


def test_embed_text_openai_failure_reason(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "https://api.example.com/v1")
    with patch("urllib.request.urlopen", side_effect=OSError("nope")):
        assert emb.embed_text("q") is None
    assert emb._last_embed_failure_reason == "embed_request_failed"


def test_embed_text_empty_embedding_field_returns_falsey_and_no_reason_reset():
    # Ollama returns 200 but no vector — embed_text returns the empty value as-is.
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": []})):
        out = emb.embed_text("weird")
    assert not out  # None or [] — nothing cached, host not marked success


# --------------------------------------------------------------------------
# embed_batch paths
# --------------------------------------------------------------------------
def test_embed_batch_empty_returns_empty():
    assert emb.embed_batch([]) == []


def test_embed_batch_disabled(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    with patch("urllib.request.urlopen") as m:
        out = emb.embed_batch(["a", "b"])
    assert out == [None, None]
    m.assert_not_called()


def test_embed_batch_ollama_single_request_and_blank_skipped():
    vecs = [[0.1] * 768, [0.2] * 768]
    resp = _mock_response({"embeddings": vecs})
    with patch("urllib.request.urlopen", return_value=resp) as m:
        out = emb.embed_batch(["a", "", "b"])
    assert out[0] == _unit(vecs[0])
    assert out[1] is None  # blank input never embedded
    assert out[2] == _unit(vecs[1])
    body = json.loads(m.call_args[0][0].data.decode())
    assert body["input"] == ["a", "b"]  # only non-blank misses sent
    assert m.call_args[0][0].full_url.endswith("/api/embed")


def test_embed_batch_all_cached_skips_http():
    raw = [0.3] * 768
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": raw})):
        emb.embed_text("cached")
    with patch("urllib.request.urlopen") as m:
        out = emb.embed_batch(["cached"])
    m.assert_not_called()
    assert out == [_unit(raw)]


def test_embed_batch_cooldown_returns_partial():
    _, base = emb.resolve_embed_backend()
    emb._dead_hosts[emb._host_key(base)] = time.time() + 100
    with patch("urllib.request.urlopen") as m:
        out = emb.embed_batch(["a", "b"])
    m.assert_not_called()
    assert out == [None, None]
    assert emb._last_embed_failure_reason == "ollama_host_cooldown"


def test_embed_batch_openai_backend(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "https://api.example.com/v1")
    monkeypatch.setenv("ENGRAM_EMBED_API_KEY", "sk-batch")
    vecs = [[0.4] * 1024, [0.6] * 1024]
    resp = _mock_response({"data": [{"embedding": vecs[0]}, {"embedding": vecs[1]}]})
    with patch("urllib.request.urlopen", return_value=resp) as m:
        out = emb.embed_batch(["a", "b"])
    assert out == [_unit(vecs[0]), _unit(vecs[1])]
    req = m.call_args[0][0]
    assert req.full_url == "https://api.example.com/v1/embeddings"
    assert req.get_header("Authorization") == "Bearer sk-batch"


def test_embed_batch_openai_backend_no_api_key(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "https://api.example.com/v1")
    vecs = [[0.4] * 1024]
    resp = _mock_response({"data": [{"embedding": vecs[0]}]})
    with patch("urllib.request.urlopen", return_value=resp) as m:
        out = emb.embed_batch(["a"])
    assert out == [_unit(vecs[0])]
    assert m.call_args[0][0].get_header("Authorization") is None


def test_embed_batch_skips_falsey_vectors_in_result():
    vec = [0.5] * 768
    # Batch returns one empty vector and one real one for two inputs.
    resp = _mock_response({"embeddings": [[], vec]})
    with patch("urllib.request.urlopen", return_value=resp):
        out = emb.embed_batch(["a", "b"])
    assert out[0] is None  # falsey vector left as None, not cached
    assert out[1] == _unit(vec)


def test_embed_batch_falls_back_per_item_on_http_error():
    vec = [0.7] * 768
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("batch endpoint missing")
        return _mock_response({"embedding": vec})

    with patch("urllib.request.urlopen", side_effect=flaky):
        out = emb.embed_batch(["x", "y"])
    assert out == [_unit(vec), _unit(vec)]
    assert calls["n"] == 3  # 1 failed batch + 2 per-item embeds


def test_embed_batch_falls_back_on_count_mismatch():
    vec = [0.8] * 768
    calls = {"n": 0}

    def responder(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # batch endpoint returns too few vectors → count mismatch
            return _mock_response({"embeddings": [vec]})
        return _mock_response({"embedding": vec})

    with patch("urllib.request.urlopen", side_effect=responder):
        out = emb.embed_batch(["p", "q"])
    assert out == [_unit(vec), _unit(vec)]
    assert calls["n"] == 3  # batch (mismatch) + 2 fallbacks


# --------------------------------------------------------------------------
# persistent (L2) cache
# --------------------------------------------------------------------------
def test_persistent_cache_path_off_returns_none(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    assert emb._persistent_cache_path() is None


def test_persistent_cache_path_default_location(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "on")
    monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "sub" / "memory.db"))
    path = emb._persistent_cache_path()
    assert path == str(tmp_path / "sub" / "embed_cache.db")


def test_persistent_cache_survives_cold_lru(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "on")
    raw = [0.9] * 768
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": raw})) as m:
        first = emb.embed_text("persist me")
    assert m.call_count == 1

    emb.clear_embedding_cache()  # simulate a fresh process (cold in-proc LRU)
    with patch("urllib.request.urlopen") as m2:
        second = emb.embed_text("persist me")
    m2.assert_not_called()
    assert second == first


def test_persistent_cache_put_evicts_oldest_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "on")
    monkeypatch.setattr(emb, "PERSISTENT_CACHE_MAX_ROWS", 2)
    for i in range(4):
        emb._persistent_cache_put("m", f"text {i}", [float(i)] * 3)
    conn = emb._cache_conn()
    n = conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
    conn.close()
    assert n == 2  # trimmed down to the cap


def test_persistent_cache_get_swallows_errors(monkeypatch):
    monkeypatch.setattr(emb, "_cache_conn", MagicMock(side_effect=RuntimeError("boom")))
    assert emb._persistent_cache_get("m", "t") is None


def test_persistent_cache_put_swallows_errors(monkeypatch):
    monkeypatch.setattr(emb, "_cache_conn", MagicMock(side_effect=RuntimeError("boom")))
    # Must not raise despite the failing connection.
    emb._persistent_cache_put("m", "t", [0.1, 0.2])


def test_cache_key_hash_is_stable_and_model_sensitive():
    a = emb._cache_key_hash("m1", "text")
    b = emb._cache_key_hash("m1", "text")
    c = emb._cache_key_hash("m2", "text")
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def test_clear_embedding_cache_empties_lru():
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": [0.1] * 768})):
        emb.embed_text("something")
    assert len(emb._embed_cache) == 1
    emb.clear_embedding_cache()
    assert len(emb._embed_cache) == 0


# ── warm_up: prime the model before a batch (cold-start cascade fix) ──

def test_warm_up_disabled_returns_false(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    assert emb.warm_up() is False


def test_warm_up_success_clears_cooldown():
    _, base = emb.resolve_embed_backend()
    emb._dead_hosts[emb._host_key(base)] = time.time() + 100  # pretend host is dead
    emb._host_fails[emb._host_key(base)] = 5
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": [0.3] * 768})):
        assert emb.warm_up() is True
    # a deliberate warm-up must lift a stale cooldown so the batch can proceed
    assert emb._host_key(base) not in emb._dead_hosts
    assert emb._host_key(base) not in emb._host_fails


def test_warm_up_failure_returns_false():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("boom")):
        assert emb.warm_up() is False


def test_embed_text_forwards_custom_timeout():
    with patch("urllib.request.urlopen", return_value=_mock_response({"embedding": [0.1] * 768})) as m:
        emb.embed_text("hi", timeout=emb.WARMUP_TIMEOUT)
    # the long warm-up timeout must reach urlopen, not the default _EMBED_TIMEOUT
    assert m.call_args.kwargs.get("timeout") == emb.WARMUP_TIMEOUT
