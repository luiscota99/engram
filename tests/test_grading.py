"""Unit tests for benchmarks/grading.py."""

from __future__ import annotations

from benchmarks.grading import (
    row_matches_expected,
    use_id_grading,
)


def test_use_id_grading_requires_id_and_type():
    assert use_id_grading({"expected_type": "skill", "expected_item_id": 1}) is True
    assert use_id_grading({"expected_type": "skill"}) is False
    assert use_id_grading({"expected_item_id": 1}) is False


def test_row_matches_by_id():
    q = {"expected_type": "mistake", "expected_item_id": 2, "expected_title_contains": "x"}
    assert row_matches_expected({"item_type": "mistake", "item_id": "2", "title": "nope"}, q) is True
    assert row_matches_expected({"item_type": "mistake", "item_id": 3, "title": "nope"}, q) is False


def test_row_matches_by_title_when_no_id():
    q = {"expected_type": "mistake", "expected_title_contains": "hello"}
    assert row_matches_expected({"item_type": "mistake", "item_id": 1, "title": "say hello world"}, q) is True
