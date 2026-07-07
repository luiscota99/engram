"""Tests for ranking type inference and related helpers."""
from __future__ import annotations

from src.ranking import _query_implies_ide_or_rules_prompt, infer_type_from_query


class TestIdeRulesPromptTier:
    def test_cursor_rules_with_how_to_is_prompt_not_skill(self):
        assert (
            infer_type_from_query("how to write cursor rules in .mdc")
            == "prompt"
        )

    def test_dot_cursorrules_only(self):
        assert infer_type_from_query("sync .cursorrules to team repo") == "prompt"

    def test_cursorrules_one_word(self):
        assert infer_type_from_query("edit cursorrules for newline") == "prompt"

    def test_mdc_word_boundary(self):
        assert infer_type_from_query("rename the mdc file") == "prompt"

    def test_implies_ide_false_for_unrelated(self):
        assert not _query_implies_ide_or_rules_prompt("how to run a database migration safely")


class TestInferTypeFromQueryOrdering:
    def test_how_to_migration_is_skill(self):
        assert (
            infer_type_from_query("how to run a database migration safely")
            == "skill"
        )

    def test_steps_to_is_skill_without_mistake_substring(self):
        # Note: "debugging" contains substring "bug", which matches mistake first.
        assert (
            infer_type_from_query("steps to analyze a stack trace in logs")
            == "skill"
        )

    def test_mistake_before_skill(self):
        assert "error" in "how to fix this error"
        # "error" is mistake keyword — should win before skill's "how to"
        assert infer_type_from_query("how to fix this error in deploy") == "mistake"


# ── Temporal ranking ─────────────────────────────────────────────────

class TestTemporalRanking:
    def test_detect_temporal_intent_directions(self):
        from src.query_analyzer import detect_temporal_intent

        assert detect_temporal_intent("what was the first issue with my car")["direction"] == "earliest"
        assert detect_temporal_intent("most recent database migration")["direction"] == "latest"
        assert detect_temporal_intent("fix the sqlite index")["has_temporal"] is False

    def test_detect_temporal_intent_explicit_dates(self):
        from src.query_analyzer import detect_temporal_intent

        intent = detect_temporal_intent("what happened in May 2023 with the deploy")
        assert "2023-05" in intent["dates"]
        intent = detect_temporal_intent("the 2024-03 incident")
        assert "2024-03" in intent["dates"]

    def _results(self):
        return [
            {"item_type": "conversation", "item_id": "1", "title": "old", "is_semantic": True, "utility_score": 100.0},
            {"item_type": "conversation", "item_id": "2", "title": "new", "is_semantic": True, "utility_score": 100.0},
        ]

    def test_direction_cues_do_not_change_scores(self):
        """Direction boost was benchmarked and removed; cues must be inert."""
        from src.ranking import _apply_temporal_boost

        results = self._results()
        dates = {("conversation", 1): "2023-01-05", ("conversation", 2): "2024-06-01"}
        _apply_temporal_boost(results, dates, {"direction": "earliest", "dates": [], "has_temporal": True})
        assert all(r["utility_score"] == 100.0 for r in results)

    def test_explicit_date_match_boosts(self):
        from src.ranking import TEMPORAL_DATE_MATCH_BOOST, _apply_temporal_boost

        results = self._results()
        dates = {("conversation", 1): "2023-05-14", ("conversation", 2): "2024-06-01"}
        _apply_temporal_boost(results, dates, {"direction": None, "dates": ["2023-05"], "has_temporal": True})
        assert results[0]["utility_score"] == 100.0 * TEMPORAL_DATE_MATCH_BOOST
        assert results[1]["utility_score"] == 100.0

    def test_no_dates_is_noop(self):
        from src.ranking import _apply_temporal_boost

        results = self._results()
        _apply_temporal_boost(results, {}, {"direction": "latest", "dates": [], "has_temporal": True})
        assert all(r["utility_score"] == 100.0 for r in results)

    def test_slash_dates_match_dash_prefixes(self):
        from src.ranking import TEMPORAL_DATE_MATCH_BOOST, _apply_temporal_boost

        results = self._results()
        dates = {("conversation", 1): "2023/05/20", ("conversation", 2): "2024/06/01"}
        _apply_temporal_boost(results, dates, {"direction": None, "dates": ["2023-05"], "has_temporal": True})
        assert results[0]["utility_score"] == 100.0 * TEMPORAL_DATE_MATCH_BOOST
