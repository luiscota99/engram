"""Tests for the fittable-weights registry, provenance-gated loader, and CLI."""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

from src import ranking_weights as rw


@pytest.fixture(autouse=True)
def _restore_weights():
    """Every test leaves the module constants exactly as it found them."""
    before = rw.current_weights()
    yield
    rw.apply_weights(before)


def test_registry_names_resolve_and_roundtrip():
    current = rw.current_weights()
    assert set(current) == set(rw.REGISTRY)
    previous = rw.apply_weights({"rrf_weight": 77.0, "intent_mult_mistake": 1.5})
    import src.ranking as r

    assert r.RRF_WEIGHT == 77.0
    assert r.INTENT_TYPE_MULTIPLIERS["mistake"] == 1.5
    rw.apply_weights(previous)  # restore returns the world to before
    assert rw.current_weights() == current


def test_apply_clamps_to_bounds_and_ignores_unknown():
    rw.apply_weights({"bm25_weight": 99.0, "not_a_weight": 1.0})
    import src.ranking as r

    _m, _a, _k, _lo, hi = rw.REGISTRY["bm25_weight"]
    assert r.BM25_WEIGHT == hi  # clamped


def test_loader_refuses_unproven_and_garbage(tmp_path, monkeypatch):
    path = tmp_path / "ranking_weights.json"
    monkeypatch.setenv("ENGRAM_RANKING_WEIGHTS", str(path))
    before = rw.current_weights()

    assert rw.load_and_apply_persisted() is False          # missing file
    path.write_text("not json")
    assert rw.load_and_apply_persisted() is False          # garbage
    path.write_text(json.dumps({"weights": {"rrf_weight": 70.0}, "proven": False}))
    assert rw.load_and_apply_persisted() is False          # unproven → refused
    assert rw.current_weights() == before                  # nothing moved

    path.write_text(json.dumps({"weights": {"rrf_weight": 70.0}, "proven": True}))
    assert rw.load_and_apply_persisted() is True
    import src.ranking as r

    assert r.RRF_WEIGHT == 70.0


def _capture(func, *args) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args)
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_cli_weights_apply_refuses_unproven(tmp_path, monkeypatch):
    from src.cli.commands.tools import cmd_weights

    monkeypatch.setenv("ENGRAM_RANKING_WEIGHTS", str(tmp_path / "installed.json"))
    cand = tmp_path / "cand.json"
    cand.write_text(json.dumps({"weights": {"rrf_weight": 60.0}, "proven": False}))
    with pytest.raises(SystemExit):
        _capture(cmd_weights, SimpleNamespace(weights_action="apply", file=str(cand)))
    assert not (tmp_path / "installed.json").exists()

    cand.write_text(json.dumps({"weights": {"rrf_weight": 60.0}, "proven": True}))
    out = _capture(cmd_weights, SimpleNamespace(weights_action="apply", file=str(cand)))
    assert "Installed" in out
    assert (tmp_path / "installed.json").exists()


def test_cli_weights_show_and_clear(tmp_path, monkeypatch):
    from src.cli.commands.tools import cmd_weights

    installed = tmp_path / "installed.json"
    monkeypatch.setenv("ENGRAM_RANKING_WEIGHTS", str(installed))
    out = _capture(cmd_weights, SimpleNamespace(weights_action="show"))
    assert "rrf_weight" in out and "none — code defaults" in out

    installed.write_text(json.dumps({"weights": {}, "proven": True}))
    out = _capture(cmd_weights, SimpleNamespace(weights_action="clear"))
    assert "Removed" in out
    assert not installed.exists()
