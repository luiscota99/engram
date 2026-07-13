"""Coverage tests for src/auto_extract.py — LLM + regex fact extraction."""

from unittest.mock import patch

import pytest

from src.auto_extract import (
    _clean_value,
    _llm_extract,
    _regex_extract,
    extract_from_messages,
    extract_from_task,
    format_auto_extract_result,
)

# ---------------------------------------------------------------------------
# _clean_value
# ---------------------------------------------------------------------------

def test_clean_value_collapses_whitespace_and_strips_punctuation():
    # Internal whitespace collapsed to single spaces, trailing punct stripped.
    assert _clean_value("  hello   world .,! ") == "hello world"


def test_clean_value_rejects_too_long():
    assert _clean_value("x" * 130) == ""


def test_clean_value_rejects_empty_after_strip():
    assert _clean_value("   ...   ") == ""


@pytest.mark.parametrize("bad", ["visit https://x.com", "a@b", "use {token}", "a<b>c"])
def test_clean_value_rejects_urls_emails_and_brackets(bad):
    assert _clean_value(bad) == ""


def test_clean_value_none_input():
    assert _clean_value(None) == ""


def test_clean_value_respects_custom_max_len():
    assert _clean_value("abcdef", max_len=3) == ""
    assert _clean_value("abc", max_len=3) == "abc"


# ---------------------------------------------------------------------------
# _regex_extract
# ---------------------------------------------------------------------------

def test_regex_extract_name():
    out = _regex_extract([{"role": "user", "content": "Hi, my name is Alice."}])
    assert out == [{"type": "fact", "title": "User identity",
                    "summary": "User's name is Alice."}]


def test_regex_extract_preference():
    out = _regex_extract([{"role": "user", "content": "I prefer dark mode editors."}])
    assert len(out) == 1
    assert out[0]["title"] == "User preference"
    assert out[0]["summary"] == "User prefers dark mode editors."


def test_regex_extract_always_never_rule():
    out = _regex_extract([{"role": "assistant", "content": "always rebase before merging"}])
    assert out[0]["title"] == "Project constraint"
    assert out[0]["summary"] == "Always/never rule: rebase before merging."


def test_regex_extract_dedup_identical_summaries():
    msgs = [
        {"role": "user", "content": "my name is Bob"},
        {"role": "user", "content": "my name is Bob"},
    ]
    out = _regex_extract(msgs)
    assert len(out) == 1


def test_regex_extract_skips_non_user_assistant_roles():
    out = _regex_extract([{"role": "system", "content": "my name is Ghost"}])
    assert out == []


def test_regex_extract_skips_empty_content():
    out = _regex_extract([{"role": "user", "content": ""}, {"role": "user"}])
    assert out == []


def test_regex_extract_caps_at_two():
    msgs = [
        {"role": "user", "content": "my name is Ann"},
        {"role": "user", "content": "I prefer vim"},
        {"role": "user", "content": "always run tests"},
    ]
    out = _regex_extract(msgs)
    assert len(out) == 2


def test_regex_extract_rejects_overlong_name():
    # Captured name exceeds _clean_value's max_len, so nothing is added.
    out = _regex_extract([{"role": "user", "content": "my name is " + "A" * 60}])
    assert out == []


# ---------------------------------------------------------------------------
# _llm_extract
# ---------------------------------------------------------------------------

def test_llm_extract_returns_empty_when_unavailable():
    with patch("src.auto_extract.is_llm_available", return_value=False):
        assert _llm_extract([{"role": "user", "content": "hello"}]) == []


def test_llm_extract_parses_valid_list():
    parsed = [{"type": "pattern", "title": "T", "summary": "S1"},
              {"summary": "S2"}]
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract.call_chat_completion", return_value="raw") as cc, \
         patch("src.auto_extract.parse_json_from_llm", return_value=parsed):
        out = _llm_extract([{"role": "user", "content": "x"}])
    assert out == [
        {"type": "pattern", "title": "T", "summary": "S1"},
        {"type": "fact", "title": "Extracted fact", "summary": "S2"},
    ]
    # System prompt is prepended to the chat messages sent to the LLM.
    sent = cc.call_args[0][0]
    assert sent[0]["role"] == "system"


def test_llm_extract_empty_raw_returns_empty():
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract.call_chat_completion", return_value=""):
        assert _llm_extract([{"role": "user", "content": "x"}]) == []


def test_llm_extract_non_list_parsed_returns_empty():
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract.call_chat_completion", return_value="raw"), \
         patch("src.auto_extract.parse_json_from_llm", return_value={"not": "list"}):
        assert _llm_extract([{"role": "user", "content": "x"}]) == []


def test_llm_extract_caps_at_two_and_drops_summaryless():
    parsed = [
        {"summary": "a"}, {"summary": "b"}, {"summary": "c"},
        {"title": "no summary"},
    ]
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract.call_chat_completion", return_value="raw"), \
         patch("src.auto_extract.parse_json_from_llm", return_value=parsed):
        out = _llm_extract([{"role": "user", "content": "x"}])
    assert [c["summary"] for c in out] == ["a", "b"]


def test_llm_extract_truncates_to_last_six_messages():
    parsed = []
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract.call_chat_completion", return_value="raw") as cc, \
         patch("src.auto_extract.parse_json_from_llm", return_value=parsed):
        _llm_extract(msgs)
    sent = cc.call_args[0][0]
    # 1 system + 6 recent messages
    assert len(sent) == 7
    assert sent[1]["content"] == "m4"


# ---------------------------------------------------------------------------
# extract_from_messages
# ---------------------------------------------------------------------------

def test_extract_from_messages_combines_llm_and_regex():
    msgs = [{"role": "user", "content": "my name is Zoe"}]
    llm_out = [{"type": "pattern", "title": "P", "summary": "llm fact"}]
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract._llm_extract", return_value=llm_out):
        res = extract_from_messages(msgs)
    assert res["llm_used"] is True
    assert res["regex_used"] is True
    assert res["llm_available"] is True
    summaries = [c["summary"] for c in res["candidates"]]
    assert "llm fact" in summaries
    assert "User's name is Zoe." in summaries


def test_extract_from_messages_regex_only_when_no_llm():
    msgs = [{"role": "user", "content": "I prefer tabs"}]
    with patch("src.auto_extract.is_llm_available", return_value=False):
        res = extract_from_messages(msgs)
    assert res["llm_used"] is False
    assert res["regex_used"] is True
    assert res["llm_available"] is False
    assert len(res["candidates"]) == 1


def test_extract_from_messages_caps_candidates_at_four():
    llm_out = [{"type": "fact", "title": "t", "summary": f"s{i}"} for i in range(4)]
    msgs = [{"role": "user", "content": "my name is Al"}]
    with patch("src.auto_extract.is_llm_available", return_value=True), \
         patch("src.auto_extract._llm_extract", return_value=llm_out):
        res = extract_from_messages(msgs)
    assert len(res["candidates"]) == 4


# ---------------------------------------------------------------------------
# extract_from_task
# ---------------------------------------------------------------------------

def test_extract_from_task_wraps_capture_and_auto_extract():
    with patch("src.auto_extract.is_llm_available", return_value=False):
        res = extract_from_task(
            task_description="Fix flaky test",
            outcome="I prefer running pytest with -x",
            errors_encountered="AssertionError",
            files_changed=["tests/x.py"],
        )
    assert "capture_suggestion" in res
    assert "auto_extract" in res
    # capture uses the real suggest_capture heuristics
    assert isinstance(res["capture_suggestion"], dict)
    assert "suggested_types" in res["capture_suggestion"]
    # auto_extract picked up the preference from the outcome text
    summaries = [c["summary"] for c in res["auto_extract"]["candidates"]]
    assert any("prefers" in s for s in summaries)


# ---------------------------------------------------------------------------
# format_auto_extract_result
# ---------------------------------------------------------------------------

def test_format_no_candidates_reports_llm_availability():
    out = format_auto_extract_result({"candidates": [], "llm_available": True})
    assert "No durable facts detected." in out
    assert "LLM available: True" in out


def test_format_with_candidates_numbers_and_uppercases_type():
    result = {
        "candidates": [
            {"type": "pattern", "title": "WAL locks", "summary": "Use WAL mode."},
            {"type": "fact", "title": "Name", "summary": "User is Al."},
        ]
    }
    out = format_auto_extract_result(result)
    assert "1. [PATTERN] WAL locks" in out
    assert "   Use WAL mode." in out
    assert "2. [FACT] Name" in out
    assert "Present drafts to the user for approval" in out


def test_format_with_candidates_uses_defaults_for_missing_fields():
    out = format_auto_extract_result({"candidates": [{}]})
    assert "1. [FACT] Untitled" in out
