"""Tests for src/embeddings.py — Ollama embedding requests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src import embeddings as emb


@pytest.fixture(autouse=True)
def reset_embed_host_state():
    """Clear dead-host cooldown and the embedding cache between tests."""
    emb._dead_hosts.clear()
    emb._host_fails.clear()
    emb._last_embed_failure_reason = None
    emb.clear_embedding_cache()
    yield
    emb._dead_hosts.clear()
    emb._host_fails.clear()
    emb._last_embed_failure_reason = None
    emb.clear_embedding_cache()


def _unit(v):
    import math

    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def test_embed_text_success_returns_embedding_vector():
    fake_embedding = [0.1] * emb.VEC_EMBEDDING_DIMENSION
    payload = json.dumps({"embedding": fake_embedding}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = emb.embed_text("hello world")

    assert result == _unit(fake_embedding)


def test_embed_text_failure_returns_none():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        result = emb.embed_text("hello world")

    assert result is None


def test_embed_text_empty_string_returns_none():
    with patch("urllib.request.urlopen") as mock_open:
        result = emb.embed_text("")

    assert result is None
    mock_open.assert_not_called()


def _mock_response(embedding):
    payload = json.dumps({"embedding": embedding}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def test_embed_text_caches_repeated_queries():
    fake_embedding = [0.1] * emb.VEC_EMBEDDING_DIMENSION

    with patch("urllib.request.urlopen", return_value=_mock_response(fake_embedding)) as mock_open:
        first = emb.embed_text("hello world")
        second = emb.embed_text("hello world")

    assert first == _unit(fake_embedding)
    assert second == _unit(fake_embedding)
    assert mock_open.call_count == 1, "second identical call must be served from cache"


def test_embed_text_failure_is_not_cached():
    fake_embedding = [0.2] * emb.VEC_EMBEDDING_DIMENSION

    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        assert emb.embed_text("hello world") is None

    with patch("urllib.request.urlopen", return_value=_mock_response(fake_embedding)) as mock_open:
        result = emb.embed_text("hello world")

    assert result == _unit(fake_embedding)
    assert mock_open.call_count == 1, "failed embed must retry, not serve a cached None"


def test_embed_cache_evicts_least_recently_used():
    fake_embedding = [0.3] * emb.VEC_EMBEDDING_DIMENSION

    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _mock_response(fake_embedding)) as mock_open:
        for i in range(emb.EMBED_CACHE_MAX + 1):
            emb.embed_text(f"query {i}")
        assert mock_open.call_count == emb.EMBED_CACHE_MAX + 1

        # "query 0" was evicted; re-embedding it hits the network again
        emb.embed_text("query 0")
        assert mock_open.call_count == emb.EMBED_CACHE_MAX + 2

        # "query 1" survived (still within capacity after one eviction)
        emb.embed_text(f"query {emb.EMBED_CACHE_MAX}")
        assert mock_open.call_count == emb.EMBED_CACHE_MAX + 2


def test_embed_backend_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    kind, base = emb.resolve_embed_backend()
    assert kind == "ollama"
    assert "11434" in base


def test_embed_backend_disabled(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    assert emb.resolve_embed_backend()[0] == "disabled"
    assert emb.is_embedding_host_available() is False

    with patch("urllib.request.urlopen") as mock_open:
        assert emb.embed_text("hello") is None
    mock_open.assert_not_called()


def test_embed_openai_compatible_backend(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "https://api.example.com/v1")
    monkeypatch.setenv("ENGRAM_EMBED_API_KEY", "sk-test")
    fake_embedding = [0.5] * 1024

    payload = json.dumps({"data": [{"embedding": fake_embedding}]}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
        result = emb.embed_text("hello world", model="text-embedding-3-small")

    assert result == _unit(fake_embedding)
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://api.example.com/v1/embeddings"
    assert req.get_header("Authorization") == "Bearer sk-test"
    body = json.loads(req.data.decode())
    assert body["input"] == "hello world"
    assert body["model"] == "text-embedding-3-small"


def test_openai_endpoint_url_forms():
    assert emb._openai_embeddings_endpoint("https://h") == "https://h/v1/embeddings"
    assert emb._openai_embeddings_endpoint("https://h/v1") == "https://h/v1/embeddings"
    assert emb._openai_embeddings_endpoint("https://h/v1/embeddings") == "https://h/v1/embeddings"


def test_embedding_matches_vec_schema_respects_expected_dim():
    ok, err = emb.embedding_matches_vec_schema([0.1] * 512, "custom-model", expected_dim=512)
    assert ok and err is None

    ok, err = emb.embedding_matches_vec_schema([0.1] * 512, "custom-model", expected_dim=768)
    assert not ok
    assert "512" in err


def _batch_response(embeddings):
    payload = json.dumps({"embeddings": embeddings}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def test_embed_batch_single_request(monkeypatch):
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    vecs = [[0.1] * 768, [0.2] * 768, [0.3] * 768]

    with patch("urllib.request.urlopen", return_value=_batch_response(vecs)) as mock_open:
        out = emb.embed_batch(["a", "b", "c"])

    assert out == [_unit(v) for v in vecs]
    assert mock_open.call_count == 1
    req = mock_open.call_args[0][0]
    assert req.full_url.endswith("/api/embed")
    assert json.loads(req.data.decode())["input"] == ["a", "b", "c"]


def test_embed_batch_serves_cached_items_without_http(monkeypatch):
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    vec_a = [0.4] * 768

    with patch("urllib.request.urlopen", return_value=_mock_response(vec_a)):
        emb.embed_text("a")  # warm cache

    with patch("urllib.request.urlopen", return_value=_batch_response([[0.5] * 768])) as mock_open:
        out = emb.embed_batch(["a", "b"])

    assert out[0] == _unit(vec_a)
    assert out[1] == _unit([0.5] * 768)
    # only the miss ("b") went over the wire
    assert json.loads(mock_open.call_args[0][0].data.decode())["input"] == ["b"]


def test_embed_batch_falls_back_per_item_on_error(monkeypatch):
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    vec = [0.6] * 768
    calls = {"n": 0}

    def flaky_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("batch endpoint missing")
        return _mock_response(vec)

    with patch("urllib.request.urlopen", side_effect=flaky_urlopen):
        out = emb.embed_batch(["x", "y"])

    assert out == [_unit(vec), _unit(vec)]
    assert calls["n"] == 3  # 1 failed batch + 2 per-item fallbacks


def test_embed_batch_disabled_backend(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_URL", "disabled")
    with patch("urllib.request.urlopen") as mock_open:
        out = emb.embed_batch(["a", "b"])
    assert out == [None, None]
    mock_open.assert_not_called()


def test_embed_text_returns_unit_vectors():
    raw = [3.0] * emb.VEC_EMBEDDING_DIMENSION
    with patch("urllib.request.urlopen", return_value=_mock_response(raw)):
        out = emb.embed_text("normalize me")
    import math
    assert abs(math.sqrt(sum(x * x for x in out)) - 1.0) < 1e-9


def test_embed_batch_returns_unit_vectors(monkeypatch):
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    raw = [[2.0] * 768, [5.0] * 768]
    with patch("urllib.request.urlopen", return_value=_batch_response(raw)):
        out = emb.embed_batch(["a", "b"])
    import math
    for v in out:
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9


def test_embed_max_chars_override(monkeypatch):
    monkeypatch.delenv("ENGRAM_EMBED_URL", raising=False)
    monkeypatch.setenv("ENGRAM_EMBED_MAX_CHARS", "100")
    vec = [0.1] * 768
    with patch("urllib.request.urlopen", return_value=_mock_response(vec)) as mock_open:
        emb.embed_text("x" * 5000)
    body = json.loads(mock_open.call_args[0][0].data.decode())
    assert len(body["prompt"]) == 100
