"""Tests for src/capture.py — heuristic signal detection and capture suggestion."""
from __future__ import annotations

from src.capture import _infer_domain, format_capture_suggestion, suggest_capture


class TestInferDomain:
    def test_frontend_keywords(self):
        assert _infer_domain("fixing a React component with CSS issue") == "frontend"

    def test_backend_keywords(self):
        assert _infer_domain("REST API endpoint returning 500 error") == "backend"

    def test_devops_keywords(self):
        assert _infer_domain("Docker container failing to deploy in CI pipeline") == "devops"

    def test_testing_keywords(self):
        assert _infer_domain("pytest test coverage for new module") == "testing"

    def test_security_keywords(self):
        assert _infer_domain("OAuth token vulnerability audit") == "security"

    def test_performance_keywords(self):
        assert _infer_domain("slow memory usage causing bottleneck optimize the cache") == "performance"

    def test_debugging_keywords(self):
        assert _infer_domain("traceback error in production crash") == "debugging"

    def test_default_engineering(self):
        assert _infer_domain("updated some files and refactored a bit") == "engineering"


class TestSuggestCaptureDetectsMistake:
    def test_detects_draft_mistake_from_error_text(self):
        result = suggest_capture(
            task_description="Updating the image pipeline",
            outcome="Fixed the crash by adding a null check",
            errors_encountered="TypeError: NoneType has no attribute 'render' — traceback in pipeline.py",
        )
        assert "mistake" in result["suggested_types"]
        assert result["draft_mistake"] is not None
        assert result["draft_mistake"]["fix"] != ""

    def test_draft_mistake_fields_populated(self):
        result = suggest_capture(
            task_description="Deploying to production",
            outcome="The fix was to set the correct environment variable",
            errors_encountered="The root cause was a missing ENV variable causing the crash",
        )
        draft = result["draft_mistake"]
        assert draft is not None
        assert "context" in draft
        assert "mistake" in draft
        assert "fix" in draft
        assert "tags" in draft

    def test_no_mistake_without_errors(self):
        result = suggest_capture(
            task_description="Added a new feature",
            outcome="Successfully implemented the workflow steps",
            errors_encountered="",
        )
        assert result["draft_mistake"] is None


class TestSuggestCaptureDetectsSkill:
    def test_detects_draft_skill_from_workflow_text(self):
        result = suggest_capture(
            task_description="Setting up a new deployment workflow",
            outcome="Successfully completed all steps: build, test, push, deploy. The process worked perfectly.",
        )
        assert "skill" in result["suggested_types"]
        assert result["draft_skill"] is not None

    def test_skill_fields_populated(self):
        result = suggest_capture(
            task_description="How to set up Docker for the project",
            outcome="Followed the steps: install, configure, run. The workflow is now documented.",
            files_changed=["Dockerfile", "docker-compose.yml", "scripts/setup.sh"],
        )
        draft = result["draft_skill"]
        assert draft is not None
        assert "name" in draft
        assert "domain" in draft
        assert "trigger" in draft
        assert "workflow" in draft

    def test_skill_includes_key_files_when_provided(self):
        result = suggest_capture(
            task_description="Refactoring the auth module",
            outcome="Successfully completed the refactor with all tests passing.",
            files_changed=["auth.py", "test_auth.py"],
        )
        assert result["draft_skill"] is not None
        assert "auth.py" in result["draft_skill"]["key_files"]

    def test_fallback_skill_suggested_even_without_signals(self):
        result = suggest_capture(
            task_description="Did something",
            outcome="It is done now.",
        )
        assert "skill" in result["suggested_types"]


class TestSuggestCaptureDetectsPattern:
    def test_detects_pattern_from_recurring_text(self):
        result = suggest_capture(
            task_description="Debugging auth token issue",
            outcome="Resolved by refreshing the token",
            errors_encountered="This keeps happening — same bug seen before whenever tokens expire",
        )
        assert "pattern" in result["suggested_types"]
        assert result["draft_pattern"] is not None

    def test_pattern_fields_populated(self):
        result = suggest_capture(
            task_description="Another recurring database timeout",
            outcome="Fixed by adding connection pool settings",
            errors_encountered="The same error again — recurring pattern with connection limits",
        )
        draft = result["draft_pattern"]
        assert draft is not None
        assert "name" in draft
        assert "symptoms" in draft
        assert "standard_fix" in draft


class TestSuggestCaptureConfidence:
    def test_confidence_keys_match_suggested_types(self):
        result = suggest_capture(
            task_description="Fixed a broken workflow",
            outcome="The fix was straightforward once we found the root cause",
            errors_encountered="Error in pipeline — it was a missing config value",
        )
        for t in result["suggested_types"]:
            assert t in result["confidence"]
            assert 0.0 <= result["confidence"][t] <= 1.0

    def test_keywords_are_extracted(self):
        result = suggest_capture(
            task_description="Optimizing the database query performance",
            outcome="Successfully optimized by adding an index",
        )
        assert len(result["keywords"]) > 0


class TestFormatCaptureSuggestion:
    def test_output_is_string(self):
        result = suggest_capture(
            task_description="Fixed bug in the renderer",
            outcome="The fix was to call render() after initialization",
            errors_encountered="Error: renderer not initialized — crash on startup",
        )
        output = format_capture_suggestion(result)
        assert isinstance(output, str)
        assert len(output) > 0

    def test_contains_header(self):
        result = suggest_capture(
            task_description="Fixed a crash",
            outcome="Resolved by null check",
            errors_encountered="NoneType error",
        )
        output = format_capture_suggestion(result)
        assert "Engram Memory Capture Suggestion" in output

    def test_contains_approval_prompt(self):
        result = suggest_capture(
            task_description="Completed authentication workflow",
            outcome="Successfully set up OAuth2 flow with all steps working",
        )
        output = format_capture_suggestion(result)
        assert "approval" in output.lower() or "save" in output.lower()

    def test_no_signals_returns_gracefully(self):
        result = {
            "suggested_types": [],
            "draft_mistake": None,
            "draft_pattern": None,
            "draft_skill": None,
            "confidence": {},
            "keywords": [],
            "domain": "engineering",
        }
        output = format_capture_suggestion(result)
        assert isinstance(output, str)
        assert "No strong signals" in output
