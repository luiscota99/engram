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


def test_embed_text_success_returns_embedding_vector():
    fake_embedding = [0.1] * emb.VEC_EMBEDDING_DIMENSION
    payload = json.dumps({"embedding": fake_embedding}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = emb.embed_text("hello world")

    assert result == fake_embedding


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

    assert first == fake_embedding
    assert second == fake_embedding
    assert mock_open.call_count == 1, "second identical call must be served from cache"


def test_embed_text_failure_is_not_cached():
    fake_embedding = [0.2] * emb.VEC_EMBEDDING_DIMENSION

    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        assert emb.embed_text("hello world") is None

    with patch("urllib.request.urlopen", return_value=_mock_response(fake_embedding)) as mock_open:
        result = emb.embed_text("hello world")

    assert result == fake_embedding
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

    assert result == fake_embedding
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
