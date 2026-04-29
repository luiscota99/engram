"""Tests for codebase_query token matching."""
from src.codebase_query import codebase_query_tokens, score_codebase_row


def test_tokenizes_natural_language():
    q = "Where is the barcode webcam field implemented?"
    tokens = codebase_query_tokens(q)
    assert "barcode" in tokens
    assert "webcam" in tokens
    assert "field" in tokens
    assert "the" not in tokens  # len < 4, common word


def test_splits_snake_case_filename():
    tokens = codebase_query_tokens("see barcode_webcam_field.js")
    assert "barcode_webcam_field" in tokens or "barcode" in tokens
    assert "webcam" in tokens
    assert "field" in tokens or "field.js" in tokens


def test_score_prefers_more_token_hits():
    s1 = score_codebase_row(
        "addons/tcg_store/static/src/barcode_webcam_field.js",
        "Knowledge entry",
        ["barcode", "webcam"],
    )
    s2 = score_codebase_row("README.md", "Knowledge entry", ["barcode", "webcam"])
    assert s1 >= s2
