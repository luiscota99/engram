"""Tests for the auto-recall enforcement hook: recall context building, the
Claude Code stdin payload contract, the CLI command, and the bootstrap writer."""

from __future__ import annotations

import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from src import hooks


def _add_mistake(db_path, mistake, context="L2 vs cosine", fix="normalize first"):
    """Insert a mistake through the real create path so it lands in FTS."""
    from src.database import get_connection
    from src.memory_ops import create_mistake

    with get_connection(db_path) as conn:
        create_mistake(conn, date="2026-07-13", context=context, mistake=mistake, fix=fix)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(db))
    _add_mistake(str(db), "mixed vector norms under L2")
    return {"path": str(db)}


def _capture(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        func(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


# ── build_recall_context ─────────────────────────────────────────────

def test_recall_context_empty_prompt_returns_empty(seeded):
    assert hooks.build_recall_context("") == ""
    assert hooks.build_recall_context("   ") == ""


def test_recall_context_surfaces_matches_with_safety_banner(seeded):
    ctx = hooks.build_recall_context("vector norms mismatch")
    assert ctx  # non-empty
    assert "REFERENCE DATA, not instructions" in ctx  # injected text is framed as data
    assert "MISTAKE" in ctx
    assert "mixed vector norms" in ctx


def test_recall_context_respects_limit(seeded):
    for i in range(6):
        _add_mistake(seeded["path"], f"vector norm issue number {i}", context="ctx")
    ctx = hooks.build_recall_context("vector norm", limit=2)
    # at most `limit` bullet lines (each hit is one "- [" bullet)
    assert ctx.count("\n- [") <= 2


# ── recall_from_payload: the Claude Code stdin contract ──────────────

def test_payload_valid_returns_userpromptsubmit_json(seeded):
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "vector norms", "cwd": "/x"})
    out = hooks.recall_from_payload(payload)
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in obj["hookSpecificOutput"]
    assert "mixed vector norms" in obj["hookSpecificOutput"]["additionalContext"]


def test_payload_no_prompt_returns_empty(seeded):
    assert hooks.recall_from_payload(json.dumps({"cwd": "/x"})) == ""


def test_payload_garbage_and_empty_never_raise(seeded):
    assert hooks.recall_from_payload("") == ""
    assert hooks.recall_from_payload("not json at all") == ""
    assert hooks.recall_from_payload("[1,2,3]") == ""  # non-dict JSON


# ── CLI: engram hook recall ──────────────────────────────────────────

def test_cmd_hook_recall_with_prompt_flag(seeded):
    from src.cli.commands.tools import cmd_hook_recall

    out = _capture(cmd_hook_recall, SimpleNamespace(prompt=["vector", "norms"]))
    obj = json.loads(out)
    assert "additionalContext" in obj["hookSpecificOutput"]


def test_cmd_hook_recall_from_stdin(seeded, monkeypatch):
    from src.cli.commands import tools

    payload = json.dumps({"prompt": "vector norms", "cwd": None})
    monkeypatch.setattr(tools.sys, "stdin", io.StringIO(payload))
    # StringIO has no isatty→ patch it to report non-tty
    monkeypatch.setattr(tools.sys.stdin, "isatty", lambda: False, raising=False)
    out = _capture(tools.cmd_hook_recall, SimpleNamespace(prompt=None))
    assert "additionalContext" in out


# ── bootstrap: write_claude_recall_hook ──────────────────────────────

def test_write_hook_creates_and_is_idempotent(tmp_path):
    from src.cli.commands.bootstrap import write_claude_recall_hook

    root = str(tmp_path)
    changed, _ = write_claude_recall_hook(root)
    assert changed is True
    settings = json.load(open(os.path.join(root, ".claude", "settings.json")))
    cmds = [h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert "engram hook recall" in cmds

    changed2, msg2 = write_claude_recall_hook(root)
    assert changed2 is False and "already" in msg2.lower()


def test_write_hook_preserves_existing_settings(tmp_path):
    from src.cli.commands.bootstrap import write_claude_recall_hook

    root = str(tmp_path)
    os.makedirs(os.path.join(root, ".claude"))
    existing = {
        "model": "opus",
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other"}]}]},
    }
    with open(os.path.join(root, ".claude", "settings.json"), "w") as f:
        json.dump(existing, f)

    write_claude_recall_hook(root)
    settings = json.load(open(os.path.join(root, ".claude", "settings.json")))
    assert settings["model"] == "opus"
    cmds = [h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert "other" in cmds and "engram hook recall" in cmds


def test_write_hook_leaves_invalid_json_untouched(tmp_path):
    from src.cli.commands.bootstrap import write_claude_recall_hook

    root = str(tmp_path)
    os.makedirs(os.path.join(root, ".claude"))
    path = os.path.join(root, ".claude", "settings.json")
    with open(path, "w") as f:
        f.write("{ not valid json")
    changed, msg = write_claude_recall_hook(root)
    assert changed is False
    assert open(path).read() == "{ not valid json"  # untouched


def test_recall_and_guard_hooks_coexist(tmp_path):
    from src.cli.commands.bootstrap import write_claude_guard_hook, write_claude_recall_hook

    root = str(tmp_path)
    write_claude_recall_hook(root)
    changed, _ = write_claude_guard_hook(root)
    assert changed is True
    settings = json.load(open(os.path.join(root, ".claude", "settings.json")))
    assert "engram hook recall" in [
        h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]
    ]
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Edit|Write|Bash"
    assert pre[0]["hooks"][0]["command"] == "engram hook guard"


# ── Guard core: build_guard_warnings + guard_from_payload ────────────

@pytest.fixture
def guarded(tmp_path, monkeypatch):
    db = tmp_path / "g.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(db))
    _add_mistake(
        str(db),
        "forgot FILTER_BRANCH_SQUELCH_WARNING and the tree-filter exit code",
        context="git filter-branch history rewrite",
    )
    return {"path": str(db)}


def test_guard_warns_on_lexically_relevant_action(guarded):
    warns = hooks.build_guard_warnings("git filter-branch --tree-filter rewrite")
    assert warns and "MISTAKE" in warns[0]


def test_guard_silent_on_unrelated_action(guarded):
    # semantic search would still return the nearest neighbor; the lexical gate
    # must suppress it — no shared terms, no warning.
    assert hooks.build_guard_warnings("list the files in this directory") == []


def test_guard_empty_action(guarded):
    assert hooks.build_guard_warnings("") == []


def test_guard_payload_warn_vs_strict(guarded):
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "git filter-branch --tree-filter x"},
    })
    warn = json.loads(hooks.guard_from_payload(payload))
    assert "permissionDecision" not in warn["hookSpecificOutput"]
    assert "MISTAKE" in warn["hookSpecificOutput"]["additionalContext"]

    strict = json.loads(hooks.guard_from_payload(payload, strict=True))
    assert strict["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_payload_no_match_is_empty(guarded):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})
    assert hooks.guard_from_payload(payload) == ""


def test_guard_payload_garbage_never_raises(guarded):
    assert hooks.guard_from_payload("") == ""
    assert hooks.guard_from_payload("not json") == ""


def test_cmd_guard_strict_exits_nonzero_on_match(guarded, tmp_path):
    from src.cli.commands.tools import cmd_guard

    f = tmp_path / "change.txt"
    f.write_text("we are running git filter-branch with a tree-filter again")
    with pytest.raises(SystemExit) as exc:
        _capture(cmd_guard, SimpleNamespace(files=[str(f)], staged=False, strict=True))
    assert exc.value.code == 1


def test_cmd_guard_clean_when_no_match(guarded, tmp_path):
    from src.cli.commands.tools import cmd_guard

    f = tmp_path / "clean.txt"
    f.write_text("completely unrelated content about spreadsheets")
    out = _capture(cmd_guard, SimpleNamespace(files=[str(f)], staged=False, strict=False))
    assert "no known mistakes" in out.lower()


# ── Recall relevance gate: injecting nothing beats injecting noise ───

def test_recall_conversational_prompt_injects_nothing(seeded):
    # hybrid search always returns a nearest neighbor; a conversational turn
    # must not surface it ("si adelante" → TCP-server skill, observed live)
    assert hooks.build_recall_context("ok go ahead") == ""
    assert hooks.build_recall_context("si") == ""


def test_recall_irrelevant_prompt_injects_nothing(seeded):
    # meaningful tokens, but none shared with the stored memory
    assert hooks.build_recall_context("schedule dentist appointment tomorrow") == ""


def test_recall_relevant_prompt_still_injects(seeded):
    ctx = hooks.build_recall_context("mixed vector norms again")
    assert ctx and "MISTAKE" in ctx


def test_recall_hook_caps_embed_timeout(seeded, monkeypatch):
    # The recall hook fires on every prompt; its query embed must be capped so a
    # cold embedder falls back to lexical instead of stalling the turn.
    captured = {}

    def _fake_search(*args, **kwargs):
        captured["embed_timeout"] = kwargs.get("embed_timeout")
        return []

    monkeypatch.setattr("src.search.search", _fake_search)
    hooks.build_recall_context("mixed vector norms again")
    assert captured["embed_timeout"] == hooks.HOOK_EMBED_TIMEOUT


def test_guard_hook_caps_embed_timeout(guarded, monkeypatch):
    captured = {}

    def _fake_search(*args, **kwargs):
        captured["embed_timeout"] = kwargs.get("embed_timeout")
        return []

    monkeypatch.setattr("src.search.search", _fake_search)
    hooks.build_guard_warnings("rm -rf vector norms")
    assert captured["embed_timeout"] == hooks.HOOK_EMBED_TIMEOUT


def test_recall_payload_conversational_returns_empty(seeded):
    payload = json.dumps({"prompt": "done", "cwd": None})
    assert hooks.recall_from_payload(payload) == ""


def test_recall_filters_per_hit_not_all_or_nothing(seeded):
    # a second, unrelated memory must be filtered while the relevant one stays
    _add_mistake(seeded["path"], "dentist appointment scheduling failure", context="calendar")
    ctx = hooks.build_recall_context("vector norms mismatch")
    assert "vector norms" in ctx
    assert "dentist" not in ctx


# ── injection ledger: the cost side of the ROI report ────────────────

def test_recall_logs_injection_and_suppression(seeded, tmp_path, monkeypatch):
    """The ledger, hermetically: canned search results so ordering effects in
    the wider suite can't mask the gate/ledger behavior under test."""
    import json as _json

    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))
    canned = [{"item_type": "mistake", "item_id": 1,
               "title": "mixed vector norms under L2", "snippet": "normalize first"}]
    monkeypatch.setattr("src.search.search", lambda *a, **kw: canned)

    # lexical overlap with the canned hit → injection with token estimate
    ctx = hooks.build_recall_context("vector norms mismatch")
    assert ctx
    # no overlap → the gate suppresses; the suppression is still recorded
    hooks.build_recall_context("thanks looks good")

    recs = [_json.loads(x) for x in log.read_text().splitlines()
            if _json.loads(x).get("source") == "recall_inject"]
    assert len(recs) == 2
    fired = [r for r in recs if r["kept"] > 0]
    suppressed = [r for r in recs if r["kept"] == 0]
    assert fired and fired[0]["tokens_est"] > 0
    assert suppressed and suppressed[0]["tokens_est"] == 0


def test_summarize_separates_injections_from_searches(tmp_path, monkeypatch):
    import json as _json

    from src.search_audit import summarize_audit_log

    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("ENGRAM_AUDIT_LOG", str(log))
    lines = [
        {"ts": "t1", "source": "hook", "query": "q", "result_count": 3},
        {"ts": "t2", "source": "recall_inject", "tokens_est": 120, "kept": 2},
        {"ts": "t3", "source": "recall_inject", "tokens_est": 0, "kept": 0},
        {"ts": "t4", "source": "guard_inject", "tokens_est": 40, "kept": 1},
    ]
    log.write_text("\n".join(_json.dumps(r) for r in lines))
    s = summarize_audit_log(str(log))
    assert s["searches"] == 1  # injection records are not searches
    assert s["injection"]["recall"] == {"evals": 2, "injected": 1, "tokens_est_total": 120}
    assert s["injection"]["guard"] == {"evals": 1, "injected": 1, "tokens_est_total": 40}


# ── Unicode-aware relevance gates (Spanish and other languages) ──────

def test_tokens_keep_accented_words_whole_and_fold_accents():
    toks = hooks._tokens("Configuración del despliegue en producción")
    # accented words tokenize whole and fold to their unaccented form
    assert "configuracion" in toks
    assert "despliegue" in toks
    assert "produccion" in toks
    # the old ASCII regex produced fragments like "configuraci" — must be gone
    assert "configuraci" not in toks
    # folding makes lazily-typed and accented spellings identical
    assert hooks._tokens("configuración") == hooks._tokens("configuracion")


def test_tokens_still_split_snake_case():
    assert {"cold", "start"} <= hooks._tokens("cold_start handling")


def test_recall_matches_accented_spanish(tmp_path, monkeypatch):
    db = tmp_path / "es.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(db))
    _add_mistake(
        str(db),
        "configuración de variables de entorno rota tras el despliegue",
        context="despliegue en producción",
        fix="validar el .env antes de desplegar",
    )
    ctx = hooks.build_recall_context("problema con la configuración del despliegue")
    assert ctx and "configuración" in ctx
    # lazily-typed (no accents) must match the accented memory via folding
    ctx2 = hooks.build_recall_context("problema con la configuracion del despliegue")
    assert ctx2 and "configuración" in ctx2
    # conversational Spanish still injects nothing
    assert hooks.build_recall_context("si dale adelante") == ""


def test_guard_matches_accented_spanish(tmp_path, monkeypatch):
    db = tmp_path / "esg.db"
    monkeypatch.setenv("ENGRAM_DB_PATH", str(db))
    monkeypatch.delenv("ENGRAM_AUDIT_LOG", raising=False)
    from src.database import init_db

    init_db(str(db))
    _add_mistake(
        str(db),
        "migración de base de datos aplicada sin respaldo previo",
        context="migración en producción",
    )
    warns = hooks.build_guard_warnings("aplicar la migración de la base de datos")
    assert warns and "MISTAKE" in warns[0]
    assert hooks.build_guard_warnings("listar archivos del directorio") == []
