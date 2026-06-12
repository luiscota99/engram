"""Tests for src/embeddings.py — Ollama embedding requests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src import embeddings as emb


@pytest.fixture(autouse=True)
def reset_embed_host_state():
    """Clear dead-host cooldown between tests."""
    emb._dead_hosts.clear()
    emb._host_fails.clear()
    emb._last_embed_failure_reason = None
    yield
    emb._dead_hosts.clear()
    emb._host_fails.clear()
    emb._last_embed_failure_reason = None


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
