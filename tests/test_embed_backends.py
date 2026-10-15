"""Tests for the fast in-process embedding backends (static / fastembed).

The backend *code path* is exercised without the optional heavy dependencies:
selection and graceful degradation are dep-free, and the encode roundtrip uses a
fake model injected via monkeypatch (needs only numpy). Tests that require the
real model2vec/fastembed packages are skipped when they are not installed.
"""
from __future__ import annotations

import importlib.util

import pytest

from src import embeddings as e

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_MODEL2VEC = importlib.util.find_spec("model2vec") is not None


@pytest.fixture(autouse=True)
def _clean_backend_state(monkeypatch):
    """Isolate each test from backend env vars and loaded-model / cache state."""
    for var in (
        "ENGRAM_EMBED_BACKEND",
        "ENGRAM_EMBED_URL",
        "ENGRAM_EMBED_MODEL",
        "ENGRAM_EMBEDDING_MODEL",
        "ENGRAM_STATIC_EMBED_MODEL",
        "ENGRAM_FASTEMBED_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    e.clear_embedding_cache()
    e._inprocess_models.clear()
    e._last_embed_failure_reason = None
    yield
    e.clear_embedding_cache()
    e._inprocess_models.clear()


def test_backend_selection_matrix(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    assert e.resolve_embed_backend() == ("static", "")
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "model2vec")  # alias
    assert e.resolve_embed_backend() == ("static", "")
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "fastembed")
    assert e.resolve_embed_backend() == ("fastembed", "")
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "disabled")
    assert e.resolve_embed_backend() == ("disabled", "")


def test_backend_env_overrides_url(monkeypatch):
    """ENGRAM_EMBED_BACKEND wins over an ENGRAM_EMBED_URL that would pick openai."""
    monkeypatch.setenv("ENGRAM_EMBED_URL", "http://example.invalid/v1")
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    assert e.resolve_embed_backend() == ("static", "")


def test_inprocess_model_name_and_defaults(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    assert e.get_embedding_model() == e.STATIC_DEFAULT_MODEL
    monkeypatch.setenv("ENGRAM_STATIC_EMBED_MODEL", "custom/static-model")
    assert e.get_embedding_model() == "custom/static-model"

    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "fastembed")
    assert e.get_embedding_model() == e.FASTEMBED_DEFAULT_MODEL


def test_inprocess_backend_available_without_cooldown(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    # No host means no dead-host cooldown can gate availability.
    assert e.is_embedding_host_available() is True


@pytest.mark.skipif(HAS_MODEL2VEC, reason="model2vec is installed; degradation path not exercised")
def test_missing_dependency_degrades_gracefully(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    assert e.embed_text("hello") is None
    assert e.embed_batch(["a", "b"]) == [None, None]
    reason = e.get_embedding_degradation_reason() or ""
    assert "model2vec" in reason


class _FakeStaticModel:
    """Stands in for model2vec.StaticModel — returns fixed unnormalized vectors."""

    def __init__(self, dim: int = 4):
        self.dim = dim

    def encode(self, texts):
        import numpy as np

        # Distinct, deterministic, decidedly non-unit vectors per input.
        return np.asarray(
            [[float(len(t) + 1), 2.0, 0.0, 0.0] for t in texts], dtype="float32"
        )


@pytest.mark.skipif(not HAS_NUMPY, reason="numpy required for the encode roundtrip")
def test_static_roundtrip_normalized_and_cached(monkeypatch):
    import math

    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    monkeypatch.setattr(e, "_load_inprocess_model", lambda kind, model: _FakeStaticModel())

    vec = e.embed_text("hello world")
    assert vec is not None
    assert len(vec) == 4
    # Backend must return unit vectors so L2 KNN == cosine (norm contract).
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, rel_tol=1e-5)

    # Second call is served from the in-process LRU (same object, no recompute).
    again = e.embed_text("hello world")
    assert again == vec


@pytest.mark.skipif(not HAS_NUMPY, reason="numpy required for the encode roundtrip")
def test_static_batch_matches_single(monkeypatch):
    monkeypatch.setenv("ENGRAM_EMBED_BACKEND", "static")
    monkeypatch.setenv("ENGRAM_EMBED_CACHE", "off")
    monkeypatch.setattr(e, "_load_inprocess_model", lambda kind, model: _FakeStaticModel())

    batch = e.embed_batch(["alpha", "beta"])
    assert len(batch) == 2 and all(v is not None for v in batch)
    for text, bvec in zip(["alpha", "beta"], batch):
        e.clear_embedding_cache()
        assert e.embed_text(text) == pytest.approx(bvec)
