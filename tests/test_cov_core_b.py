"""Coverage tests for codebase_query, merge, ranking, memory_ops."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src import codebase_query as cq
from src import memory_ops as mo
from src import merge as mrg
from src import ranking as rk

# ─────────────────────────── codebase_query.py ───────────────────────────

def test_tokens_empty_and_whitespace():
    assert cq.codebase_query_tokens("") == []
    assert cq.codebase_query_tokens("   ") == []


def test_tokens_length_and_case_rules():
    # >=4 kept; 3-char kept only if upper or has digit; <3 dropped
    toks = cq.codebase_query_tokens("the API v2 x go running")
    assert "running" in toks
    assert "API" in toks          # 3-char uppercase kept
    assert "the" not in toks      # 3-char lowercase dropped
    assert "go" not in toks       # 2-char dropped
    assert "x" not in toks


def test_tokens_split_underscore_dot_and_dedup():
    toks = cq.codebase_query_tokens("memory_ops.py Memory_OPS")
    # underscore + dot splitting produces sub-tokens
    assert "memory" in toks
    # 'ops' is 3-char lowercase -> dropped, but 'OPS' uppercase kept
    assert "OPS" in toks
    # dedup is case-insensitive: 'memory' appears once
    assert toks.count("memory") == 1


def test_score_codebase_row():
    assert cq.score_codebase_row("a/b.py", "hello", []) == 0
    # case-insensitive, counts distinct token hits in path+summary
    n = cq.score_codebase_row("src/Ranking.py", "handles BM25 scoring", ["ranking", "bm25", "absent"])
    assert n == 2


def _seed_project(conn, path="/proj"):
    cur = conn.execute("INSERT INTO projects (name, path) VALUES (?, ?)", ("proj", path))
    return cur.lastrowid


def _seed_file(conn, pid, file_path, summary):
    conn.execute(
        "INSERT INTO codebase_knowledge (project_id, file_path, file_hash, summary) "
        "VALUES (?, ?, ?, ?)",
        (pid, file_path, "hash-" + file_path, summary),
    )


def test_fetch_empty_query_order(test_db):
    conn = test_db["conn"]
    pid = _seed_project(conn)
    _seed_file(conn, pid, "z_last.py", "z summary")
    _seed_file(conn, pid, "a_first.py", "a summary")
    # both empty-string and None mean "all rows, ordered by file_path"
    for q in ("", None):
        rows = cq.fetch_codebase_rows_for_query(conn, pid, q)
        assert [r["file_path"] for r in rows] == ["a_first.py", "z_last.py"]


def test_fetch_token_match_ranked_by_coverage(test_db):
    conn = test_db["conn"]
    pid = _seed_project(conn)
    _seed_file(conn, pid, "auth/login.py", "handles authentication and login")
    _seed_file(conn, pid, "utils/helpers.py", "misc login helpers")
    rows = cq.fetch_codebase_rows_for_query(conn, pid, "authentication login")
    # login.py matches both tokens -> ranked first
    assert rows[0]["file_path"] == "auth/login.py"
    assert len(rows) == 2


def test_fetch_phrase_fallback_when_no_valid_tokens(test_db):
    conn = test_db["conn"]
    pid = _seed_project(conn)
    _seed_file(conn, pid, "db.py", "handles ab connection")
    # "ab" has no keepable tokens -> LIKE phrase fallback
    rows = cq.fetch_codebase_rows_for_query(conn, pid, "ab")
    assert len(rows) == 1
    assert rows[0]["file_path"] == "db.py"


def test_fetch_phrase_fallback_no_match(test_db):
    conn = test_db["conn"]
    pid = _seed_project(conn)
    _seed_file(conn, pid, "db.py", "connection")
    assert cq.fetch_codebase_rows_for_query(conn, pid, "zz") == []


# ─────────────────────────────── merge.py ───────────────────────────────

def test_entry_to_text_filters_meta_and_none():
    entry = {
        "id": 5,
        "created_at": "x",
        "tags": ["t"],
        "usage_count": 3,
        "mistake": "boom",
        "fix": None,
        "context": "prod",
    }
    text = mrg._entry_to_text(entry)
    assert "  mistake: boom" in text
    assert "  context: prod" in text
    assert "id:" not in text
    assert "created_at:" not in text
    assert "usage_count:" not in text
    assert "fix:" not in text  # None skipped


def test_merge_entries_none_when_llm_empty():
    with patch.object(mrg, "call_ollama_generate", return_value=""):
        assert mrg.merge_entries({"mistake": "a"}, {"mistake": "b"}) is None


def test_merge_entries_none_when_not_dict():
    with patch.object(mrg, "call_ollama_generate", return_value="raw"), \
         patch.object(mrg, "parse_json_from_llm", return_value=["not", "dict"]):
        assert mrg.merge_entries({"mistake": "a"}, {"mistake": "b"}) is None


def test_merge_entries_success_backfills_item_type_and_drops_id():
    parsed = {"mistake": "merged", "id": 99}
    with patch.object(mrg, "call_ollama_generate", return_value="raw") as gen, \
         patch.object(mrg, "parse_json_from_llm", return_value=parsed):
        out = mrg.merge_entries(
            {"mistake": "a", "item_type": "mistake"},
            {"mistake": "b"},
            model="m",
        )
    assert out == {"mistake": "merged", "item_type": "mistake"}
    assert "id" not in out
    gen.assert_called_once()


def test_merge_available_delegates():
    with patch.object(mrg, "is_llm_available", return_value=True):
        assert mrg.merge_available() is True
    with patch.object(mrg, "is_llm_available", return_value=False):
        assert mrg.merge_available() is False


# ─────────────────────────────── ranking.py ───────────────────────────────

def test_result_key():
    assert rk.result_key({"item_type": "skill", "item_id": 7}) == "skill-7"


def test_reciprocal_rank_scores_empty():
    assert rk.reciprocal_rank_scores([], []) == {}


def test_reciprocal_rank_scores_normalized():
    sem = [{"item_type": "skill", "item_id": 1}, {"item_type": "skill", "item_id": 2}]
    lex = [{"item_type": "skill", "item_id": 1}]
    scores = rk.reciprocal_rank_scores(sem, lex, k=60)
    # item 1 appears in both lists -> highest -> normalized to 1.0
    assert scores["skill-1"] == 1.0
    assert 0.0 < scores["skill-2"] < 1.0


def test_tokenize():
    assert rk._tokenize("Hello, World_42!") == ["hello", "world", "42"]


def test_bm25_score_empty_query_zero():
    assert rk.bm25_score("", "some document", 3.0, 1, {}) == 0.0


def test_bm25_score_positive_for_overlap():
    s = rk.bm25_score("ranking", "ranking module scoring", 3.0, 2, {"ranking": 1})
    assert s > 0.0


def test_bm25_scores_empty_inputs():
    assert rk.bm25_scores("q", []) == {}
    assert rk.bm25_scores("   ", [{"item_type": "s", "item_id": 1, "title": "t"}]) == {}


def test_bm25_scores_normalized_and_zero_branch():
    results = [
        {"item_type": "skill", "item_id": 1, "title": "ranking bm25", "snippet": "scoring"},
        {"item_type": "skill", "item_id": 2, "title": "unrelated text"},
    ]
    scores = rk.bm25_scores("ranking", results)
    assert scores["skill-1"] == 1.0
    assert scores["skill-2"] == 0.0
    # query with no corpus overlap -> all zero branch
    zero = rk.bm25_scores("zzzznomatch", results)
    assert set(zero.values()) == {0.0}


def test_rerank_with_bm25_noop_on_empty():
    assert rk.rerank_with_bm25([], "q") == []
    same = [{"item_type": "s", "item_id": 1}]
    assert rk.rerank_with_bm25(same, "  ") is same


def test_rerank_with_bm25_boosts_and_sorts():
    results = [
        {"item_type": "skill", "item_id": 1, "title": "off topic", "utility_score": 100.0},
        {"item_type": "skill", "item_id": 2, "title": "ranking bm25 scoring", "utility_score": 100.0},
    ]
    out = rk.rerank_with_bm25(results, "ranking bm25")
    # item 2 gets the BM25 boost -> moves to front
    assert out[0]["item_id"] == 2
    assert out[0]["bm25_score"] == 1.0
    assert out[0]["utility_score"] == pytest.approx(100.0 * (1 + rk.BM25_WEIGHT))
    assert out[1]["utility_score"] == 100.0  # zero overlap, unchanged


def test_recency_factor_variants():
    assert rk._recency_factor(None) == 0.5
    assert rk._recency_factor("not-a-date") == 0.5
    # tz-aware path
    assert rk._recency_factor("2026-07-13T00:00:00+00:00") <= 1.0
    # naive path, today -> ~1.0. Computed at runtime: a hardcoded "today"
    # decays as real days pass and detonates in CI first (UTC runs ahead).
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    assert rk._recency_factor(today) == pytest.approx(1.0, abs=0.05)


def test_usage_boost():
    assert rk._usage_boost(0) == 0.0
    assert rk._usage_boost(1) == pytest.approx(10.0 * 0.6931, abs=0.01)
    assert rk._usage_boost(-5) == 0.0  # clamped to 0


def test_tag_boost():
    assert rk._tag_boost(None, ["py"]) == 0.0
    assert rk._tag_boost("python,fastapi", []) == 0.0
    assert rk._tag_boost("python,FastAPI", ["python", "fastapi", "rust"]) == 2 * rk.TAG_MATCH_BOOST


def test_calculate_utility_score_lexical_cold():
    # unknown item_type -> rank_multiplier 1.0; cold (never used) recency 0.5
    score = rk.calculate_utility_score({"item_type": "zzz", "is_semantic": False})
    assert score == pytest.approx(50.0 * (rk.RECENCY_FLOOR + rk.RECENCY_SPAN * 0.5))


def test_calculate_utility_score_semantic_higher_than_lexical():
    sem = rk.calculate_utility_score({"item_type": "zzz", "is_semantic": True})
    lex = rk.calculate_utility_score({"item_type": "zzz", "is_semantic": False})
    assert sem > lex


def test_calculate_utility_score_stale_penalty_and_affinity():
    base = rk.calculate_utility_score({"item_type": "zzz", "is_semantic": False})
    stale = rk.calculate_utility_score(
        {"item_type": "zzz", "is_semantic": False}, embedding_is_stale=True
    )
    assert stale == pytest.approx(base - rk.STALE_EMBEDDING_PENALTY)
    created = rk.calculate_utility_score(
        {"item_type": "zzz", "is_semantic": False}, affinity="created"
    )
    assert created == pytest.approx(base + rk.AFFINITY_BOOSTS["created"])


def test_calculate_utility_score_type_match_multiplier():
    matched = rk.calculate_utility_score(
        {"item_type": "mistake", "is_semantic": False}, inferred_type="mistake"
    )
    unmatched = rk.calculate_utility_score(
        {"item_type": "mistake", "is_semantic": False}, inferred_type=None
    )
    # matched applies +TYPE_MATCH_BOOST and the intent multiplier
    assert matched > unmatched


def test_calculate_utility_score_tag_match():
    with_tag = rk.calculate_utility_score(
        {"item_type": "zzz", "is_semantic": False, "tags": "python,fastapi"},
        detected_tags=["python"],
    )
    without = rk.calculate_utility_score({"item_type": "zzz", "is_semantic": False})
    assert with_tag == pytest.approx(without + rk.TAG_MATCH_BOOST)


@pytest.mark.parametrize("query,expected", [
    ("I hit a bug in the code", "mistake"),
    ("this is a recurring pattern", "pattern"),
    ("how to deploy the app", "skill"),
    ("what did we discuss in the session", "conversation"),
    ("write a system prompt", "prompt"),
    ("edit the rules.mdc file", "prompt"),
    ("just some neutral text", None),
])
def test_infer_type_from_query(query, expected):
    assert rk.infer_type_from_query(query) == expected


def test_query_implies_ide_rules():
    assert rk._query_implies_ide_or_rules_prompt("open .cursorrules") is True
    # bare "cursorrules" token (no leading dot) hits the standalone check
    assert rk._query_implies_ide_or_rules_prompt("cursorrules config") is True
    assert rk._query_implies_ide_or_rules_prompt("the mdc file") is True
    assert rk._query_implies_ide_or_rules_prompt("a cursor rule here") is True
    assert rk._query_implies_ide_or_rules_prompt("nothing relevant") is False


def test_rank_results_sorts_and_annotates():
    results = [
        {"item_type": "skill", "item_id": 1, "is_semantic": False},
        {"item_type": "skill", "item_id": 2, "is_semantic": True},
    ]
    out = rk.rank_results(
        results,
        usage_counts={("skill", 1): 0, ("skill", 2): 0},
        last_used_map={},
        affinities={},
        query="",
    )
    # semantic item (id 2) outranks lexical
    assert out[0]["item_id"] == 2
    assert all("utility_score" in r for r in out)
    assert out[0]["rrf_normalized"] == 0.0


def test_rank_results_with_rrf():
    results = [{"item_type": "skill", "item_id": 1, "is_semantic": False}]
    rrf = {"skill-1": 1.0}
    out = rk.rank_results(
        results, {}, {}, {}, query="", rrf_scores=rrf,
    )
    assert out[0]["rrf_normalized"] == 1.0
    # utility_score includes RRF_WEIGHT contribution
    assert out[0]["utility_score"] > rk.RRF_WEIGHT * 0.5


def test_rank_results_temporal_boost():
    results = [
        {"item_type": "skill", "item_id": 1, "is_semantic": False},
        {"item_type": "skill", "item_id": 2, "is_semantic": False},
    ]
    item_dates = {("skill", 1): "2023-05-14", ("skill", 2): "2024-01-01"}
    intent = {"has_temporal": True, "dates": ["2023-05"]}
    out = rk.rank_results(
        results, {}, {}, {}, query="",
        item_dates=item_dates, temporal_intent=intent,
    )
    boosted = next(r for r in out if r["item_id"] == 1)
    assert boosted.get("temporal_boost") == "date_match"


def test_apply_temporal_boost_slash_separators():
    results = [{"item_type": "skill", "item_id": 1, "utility_score": 10.0}]
    item_dates = {("skill", 1): "2023/05/20"}
    rk._apply_temporal_boost(results, item_dates, {"dates": ["2023-05"]})
    assert results[0]["utility_score"] == pytest.approx(10.0 * rk.TEMPORAL_DATE_MATCH_BOOST)
    assert results[0]["temporal_boost"] == "date_match"


def test_apply_temporal_boost_no_dated_rows_noop():
    results = [{"item_type": "skill", "item_id": 1, "utility_score": 10.0}]
    rk._apply_temporal_boost(results, {}, {"dates": ["2023-05"]})
    assert results[0]["utility_score"] == 10.0
    assert "temporal_boost" not in results[0]


def test_apply_temporal_boost_swallows_bad_item_id():
    # non-integer item_id raises ValueError in int() -> caught, row treated undated
    results = [{"item_type": "skill", "item_id": "notanint", "utility_score": 10.0}]
    rk._apply_temporal_boost(results, {("skill", 1): "2023-05-01"}, {"dates": ["2023-05"]})
    assert results[0]["utility_score"] == 10.0


# ─────────────────────────────── memory_ops.py ───────────────────────────────

def test_parse_tags():
    assert mo._parse_tags(None) == []
    assert mo._parse_tags("a, b ,, c") == ["a", "b", "c"]
    assert mo._parse_tags(["x", " y ", ""]) == ["x", "y"]


def test_dedup_content_helpers():
    assert mo.mistake_dedup_content("ctx", "m", None, "fix", None) == "ctx | m |  | fix | "
    assert mo.pattern_dedup_content("s", "rc", "sf") == "s | rc | sf"
    assert mo.skill_dedup_content("t", "w", None) == "t | w | "


def _fts_row(conn, item_type, item_id):
    # index_in_fts stores item_id as TEXT
    return conn.execute(
        "SELECT title, content FROM memory_fts WHERE item_type=? AND item_id=?",
        (item_type, str(item_id)),
    ).fetchone()


def test_create_mistake(test_db):
    conn = test_db["conn"]
    mid = mo.create_mistake(
        conn, date="2026-01-01", context="prod", mistake="broke thing",
        fix="rollback", root_cause="typo", prevention="review", tags="a,b",
    )
    row = conn.execute("SELECT * FROM mistakes WHERE id=?", (mid,)).fetchone()
    assert row["mistake"] == "broke thing"
    assert row["fix"] == "rollback"
    fts = _fts_row(conn, "mistake", mid)
    assert fts["title"] == "broke thing"
    assert "typo" in fts["content"]


def test_create_pattern(test_db):
    conn = test_db["conn"]
    pid = mo.create_pattern(
        conn, name="N+1", symptoms="slow", root_cause="loop query",
        standard_fix="eager load", tags=["perf"],
    )
    row = conn.execute("SELECT * FROM patterns WHERE id=?", (pid,)).fetchone()
    assert row["name"] == "N+1"
    fts = _fts_row(conn, "pattern", pid)
    assert fts["title"] == "N+1"
    assert "eager load" in fts["content"]


def test_create_skill(test_db):
    conn = test_db["conn"]
    sid = mo.create_skill(
        conn, name="deploy", domain="ops", trigger="on release",
        workflow="build then ship", pitfalls="forgot migrations",
    )
    row = conn.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    assert row["name"] == "deploy"
    assert row["trigger_desc"] == "on release"
    fts = _fts_row(conn, "skill", sid)
    assert "build then ship" in fts["content"]


def test_create_conversation(test_db):
    conn = test_db["conn"]
    cid = mo.create_conversation(
        conn, conversation_id="c1", title="Chat", date="2026-01-01",
        domain="eng", tasks_completed="did x", key_decisions="chose y",
    )
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Chat"
    assert row["conversation_id"] == "c1"
    fts = _fts_row(conn, "conversation", cid)
    assert "chose y" in fts["content"]


def test_create_conversation_chunked_short_no_parts(test_db):
    conn = test_db["conn"]
    ids = mo.create_conversation_chunked(
        conn, conversation_id="s1", title="Short", date="2026-01-01",
        domain="eng", turns=["t1", "t2"], window=8,
    )
    assert len(ids) == 1


def test_create_conversation_chunked_long_makes_parts(test_db):
    conn = test_db["conn"]
    # 18 turns with window 8 / stride 4 makes a tiny final chunk that triggers
    # the "tail already covered" break.
    turns = [f"turn {i}" for i in range(18)]
    ids = mo.create_conversation_chunked(
        conn, conversation_id="s2", title="Long", date="2026-01-01",
        domain="eng", turns=turns, window=8, stride=4,
    )
    assert len(ids) > 1
    # part rows carry the "#p" suffix in conversation_id
    parts = conn.execute(
        "SELECT conversation_id FROM conversations WHERE conversation_id LIKE 's2#p%'"
    ).fetchall()
    assert len(parts) == len(ids) - 1


def test_create_session_and_add_decision(test_db):
    conn = test_db["conn"]
    sid = mo.create_session(
        conn, session_id="sess1", title="Session", date="2026-01-01",
        domain="eng", workflow_used="tdd",
    )
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["session_id"] == "sess1"
    fts = _fts_row(conn, "session", sid)
    assert "tdd" in fts["content"]

    mo.add_decision(conn, session_id="sess1", decision="use postgres")
    updated = conn.execute(
        "SELECT key_decisions FROM sessions WHERE session_id='sess1'"
    ).fetchone()
    assert "use postgres" in updated["key_decisions"]


def test_create_transcript(test_db):
    conn = test_db["conn"]
    # session_transcripts has a FK to sessions(session_id)
    mo.create_session(
        conn, session_id="sess9", title="S", date="2026-01-01", domain="eng",
    )
    mo.create_transcript(conn, session_id="sess9", role="user", content="hi there")
    row = conn.execute(
        "SELECT * FROM session_transcripts WHERE session_id='sess9'"
    ).fetchone()
    assert row["role"] == "user"
    assert row["content"] == "hi there"


def test_create_prompt(test_db):
    conn = test_db["conn"]
    pid = mo.create_prompt(
        conn, name="reviewer", role="critic", domain="code",
        description="reviews PRs", prompt_text="Be terse", best_for="reviews",
        tags="review",
    )
    row = conn.execute("SELECT * FROM prompts WHERE id=?", (pid,)).fetchone()
    assert row["name"] == "reviewer"
    assert row["prompt_text"] == "Be terse"
    fts = _fts_row(conn, "prompt", pid)
    assert "reviews PRs" in fts["content"]
