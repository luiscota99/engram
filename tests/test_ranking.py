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
