"""Coverage tests for src/summarize.py and src/compression.py.

summarize.py talks to Ollama over urllib; all network I/O is mocked at the
urllib boundary so tests are hermetic. compression.py is pure regex/string
logic exercised directly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src import compression, summarize


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _mock_generate_response(response_text: str) -> MagicMock:
    """Build a context-manager mock mimicking urlopen for /api/generate."""
    payload = json.dumps({"response": response_text}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


# --------------------------------------------------------------------------
# _detect_language
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/a/b/foo.py", "Python"),
        ("foo.JS", "JavaScript"),
        ("x.ts", "TypeScript"),
        ("main.go", "Go"),
        ("lib.rs", "Rust"),
        ("readme.md", "Markdown"),
        ("data.yaml", "YAML"),
        ("data.yml", "YAML"),
        ("conf.toml", "TOML"),
        ("noext", "text"),
        ("weird.xyz", "text"),
    ],
)
def test_detect_language(path, expected):
    assert summarize._detect_language(path) == expected


# --------------------------------------------------------------------------
# _parse_llm_json
# --------------------------------------------------------------------------
def test_parse_llm_json_plain():
    assert summarize._parse_llm_json('{"a": 1}') == {"a": 1}


def test_parse_llm_json_empty_returns_none():
    assert summarize._parse_llm_json("") is None


def test_parse_llm_json_json_fence():
    raw = 'prefix ```json\n{"summary": "hi"}\n``` suffix'
    assert summarize._parse_llm_json(raw) == {"summary": "hi"}


def test_parse_llm_json_generic_fence():
    raw = 'text ```\n{"k": "v"}\n``` more'
    assert summarize._parse_llm_json(raw) == {"k": "v"}


def test_parse_llm_json_invalid_returns_none():
    assert summarize._parse_llm_json("this is not json at all") is None


# --------------------------------------------------------------------------
# _call_ollama
# --------------------------------------------------------------------------
def test_call_ollama_success_returns_stripped_response():
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_generate_response("  hello  "),
    ):
        assert summarize._call_ollama("prompt", "llama3.2") == "hello"


def test_call_ollama_missing_response_key_returns_empty_string():
    payload = json.dumps({"other": "x"}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = payload
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_response):
        assert summarize._call_ollama("prompt", "llama3.2") == ""


def test_call_ollama_network_error_returns_none():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert summarize._call_ollama("prompt", "llama3.2") is None


def test_call_ollama_posts_expected_payload():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode())
        return _mock_generate_response("ok")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        summarize._call_ollama("my-prompt", "custom-model")

    assert captured["url"].endswith("/api/generate")
    assert captured["data"] == {
        "model": "custom-model",
        "prompt": "my-prompt",
        "stream": False,
    }


# --------------------------------------------------------------------------
# summarize_file
# --------------------------------------------------------------------------
def _valid_llm_json() -> str:
    return json.dumps(
        {
            "summary": "Does a thing.",
            "exports": ["foo", "bar"],
            "dependencies": ["os"],
            "complexity": "high",
        }
    )


def test_summarize_file_success(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("def foo():\n    return 1\n")

    with patch("urllib.request.urlopen", return_value=_mock_generate_response(_valid_llm_json())):
        result = summarize.summarize_file(str(f))

    assert result is not None
    assert result["summary"] == "Does a thing."
    # exports/dependencies are JSON-serialized strings
    assert json.loads(result["exports"]) == ["foo", "bar"]
    assert json.loads(result["dependencies"]) == ["os"]
    assert result["complexity"] == "high"


def test_summarize_file_missing_file_returns_none(tmp_path):
    missing = tmp_path / "nope.py"
    # No urlopen patch needed; OSError should short-circuit before any call.
    assert summarize.summarize_file(str(missing)) is None


def test_summarize_file_empty_content_returns_none(tmp_path):
    f = tmp_path / "empty.py"
    f.write_text("   \n\t  \n")
    assert summarize.summarize_file(str(f)) is None


def test_summarize_file_ollama_none_returns_none(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert summarize.summarize_file(str(f)) is None


def test_summarize_file_parse_failure_returns_none(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    with patch("urllib.request.urlopen", return_value=_mock_generate_response("not json")):
        assert summarize.summarize_file(str(f)) is None


def test_summarize_file_missing_summary_key_returns_none(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    bad = json.dumps({"exports": []})
    with patch("urllib.request.urlopen", return_value=_mock_generate_response(bad)):
        assert summarize.summarize_file(str(f)) is None


def test_summarize_file_defaults_missing_optional_fields(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    minimal = json.dumps({"summary": "just summary"})
    with patch("urllib.request.urlopen", return_value=_mock_generate_response(minimal)):
        result = summarize.summarize_file(str(f))
    assert result["summary"] == "just summary"
    assert json.loads(result["exports"]) == []
    assert json.loads(result["dependencies"]) == []
    assert result["complexity"] == "medium"


def test_summarize_file_uses_relative_path_in_prompt(tmp_path):
    """project_root triggers relpath; verify it reaches the prompt sent to Ollama."""
    root = tmp_path
    f = root / "pkg" / "mod.py"
    f.parent.mkdir()
    f.write_text("x = 1\n")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode())
        return _mock_generate_response(_valid_llm_json())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        summarize.summarize_file(str(f), project_root=str(root))

    assert "pkg/mod.py" in captured["data"]["prompt"]
    assert "File: pkg/mod.py" in captured["data"]["prompt"]


def test_summarize_file_respects_max_chars(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("A" * 5000)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode())
        return _mock_generate_response(_valid_llm_json())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        summarize.summarize_file(str(f), max_chars=100)

    # Only the first 100 chars of content should appear in the prompt.
    assert "A" * 100 in captured["data"]["prompt"]
    assert "A" * 101 not in captured["data"]["prompt"]


# --------------------------------------------------------------------------
# summarize_files_batch
# --------------------------------------------------------------------------
def test_summarize_files_batch_maps_paths_and_reports_progress(tmp_path):
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.py"
    f1.write_text("x = 1\n")
    f2.write_text("y = 2\n")
    progress = []

    with patch("urllib.request.urlopen", return_value=_mock_generate_response(_valid_llm_json())):
        with patch("time.sleep") as mock_sleep:
            results = summarize.summarize_files_batch(
                [str(f1), str(f2)],
                rate_limit_seconds=0.5,
                progress_callback=lambda cur, total, fp: progress.append((cur, total, fp)),
            )

    assert set(results.keys()) == {str(f1), str(f2)}
    assert results[str(f1)]["summary"] == "Does a thing."
    # progress reported once per file, 1-indexed
    assert progress == [(1, 2, str(f1)), (2, 2, str(f2))]
    # sleep called between files but not after the last one
    assert mock_sleep.call_count == 1


def test_summarize_files_batch_empty_list():
    with patch("time.sleep") as mock_sleep:
        results = summarize.summarize_files_batch([])
    assert results == {}
    assert mock_sleep.call_count == 0


def test_summarize_files_batch_records_none_on_failure(tmp_path):
    missing = tmp_path / "gone.py"
    results = summarize.summarize_files_batch([str(missing)])
    assert results == {str(missing): None}


# --------------------------------------------------------------------------
# ollama_available
# --------------------------------------------------------------------------
def test_ollama_available_true():
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_response):
        assert summarize.ollama_available() is True


def test_ollama_available_non_200():
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_response):
        assert summarize.ollama_available() is False


def test_ollama_available_error():
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert summarize.ollama_available() is False


# ==========================================================================
# compression.py
# ==========================================================================
def test_compress_lite_removes_fillers_and_hedging():
    text = "I think this is basically just a simple, actually really nice test."
    out = compression.compress_lite(text)
    for word in ("I think", "basically", "just", "actually", "really"):
        assert word not in out
    assert "simple" in out and "nice" in out
    # no double spaces left behind
    assert "  " not in out


def test_compress_lite_collapses_whitespace():
    assert compression.compress_lite("a   b\n\tc") == "a b c"


def test_compress_full_regex_removes_articles():
    text = "The cat sat on a mat under an umbrella."
    out = compression.compress_full_regex(text)
    assert "cat" in out and "mat" in out and "umbrella" in out
    # standalone articles gone
    assert " the " not in out.lower()
    assert not out.lower().startswith("the ")
    assert " a " not in f" {out.lower()} "
    assert " an " not in f" {out.lower()} "


def test_compress_full_regex_keeps_words_containing_articles():
    # "than" contains "an" but should not be stripped (word boundary).
    out = compression.compress_full_regex("rather than this")
    assert "than" in out


@pytest.mark.parametrize("level", ["lite", "full", "ultra"])
def test_get_caveman_prompt_includes_level_and_text(level):
    prompt = compression.get_caveman_prompt("hello world", level=level)
    assert f"level: {level}" in prompt
    assert "hello world" in prompt
    assert "Keep ALL technical terms" in prompt


def test_get_caveman_prompt_lite_instruction():
    prompt = compression.get_caveman_prompt("x", level="lite")
    assert "Keep articles and full sentences" in prompt


def test_get_caveman_prompt_ultra_instruction():
    prompt = compression.get_caveman_prompt("x", level="ultra")
    assert "Abbreviate" in prompt and "arrows for causality" in prompt


def test_get_caveman_prompt_unknown_level_falls_back_to_full():
    prompt = compression.get_caveman_prompt("x", level="bogus")
    # unknown level -> uses the 'full' instruction text
    assert "Drop articles" in prompt


def test_compress_caveman_lite_delegates_to_lite():
    text = "I think this really works."
    assert compression.compress_caveman(text, "lite") == compression.compress_lite(text)


def test_compress_caveman_full_delegates_to_full_regex():
    text = "The really simple thing."
    assert compression.compress_caveman(text, "full") == compression.compress_full_regex(text)


def test_compress_caveman_ultra_uses_full_regex_baseline():
    text = "The really simple thing."
    assert compression.compress_caveman(text, "ultra") == compression.compress_full_regex(text)
