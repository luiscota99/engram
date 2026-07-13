"""Coverage tests for src/graph.py — relationship extraction and formatters."""

import json
import os

from src.database import get_connection
from src.graph import (
    _extract_js_imports,
    _extract_python_imports,
    _resolve_module_path,
    _resolve_relative_js,
    _sanitize_mermaid_id,
    _short_label,
    format_dot,
    format_json,
    format_mermaid,
    index_file_relationships,
    query_relationships,
)

# ── Fixtures / helpers ───────────────────────────────────────────────


def _make_py_project(tmp_path):
    """Create a small python project with internal imports and return its root."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "b.py").write_text("x = 1\n")
    (root / "c.py").write_text("y = 2\n")
    # a.py imports b (Import) and c (ImportFrom), plus external os (unresolved)
    (root / "a.py").write_text(
        "import os\n"
        "import b\n"
        "from c import y\n"
    )
    return root


# ── _extract_python_imports ──────────────────────────────────────────


def test_extract_python_imports_finds_internal(tmp_path):
    root = _make_py_project(tmp_path)
    rels = _extract_python_imports(str(root / "a.py"), str(root))
    targets = {r["target"] for r in rels}
    # b.py via `import b`, c.py via `from c import y`; os is external and skipped
    assert targets == {"b.py", "c.py"}
    for r in rels:
        assert r["source"] == "a.py"
        assert r["type"] == "imports"


def test_extract_python_imports_syntax_error_returns_empty(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    bad = root / "bad.py"
    bad.write_text("def broken(:\n    pass\n")  # invalid syntax
    assert _extract_python_imports(str(bad), str(root)) == []


def test_extract_python_imports_missing_file_returns_empty(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    missing = root / "nope.py"
    assert _extract_python_imports(str(missing), str(root)) == []


def test_extract_python_imports_ignores_relative_from(tmp_path):
    # `from . import x` has node.module == None → skipped
    root = tmp_path / "proj"
    root.mkdir()
    (root / "m.py").write_text("from . import something\n")
    assert _extract_python_imports(str(root / "m.py"), str(root)) == []


# ── _extract_js_imports ──────────────────────────────────────────────


def test_extract_js_imports_relative_only(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "util.js").write_text("export const u = 1;\n")
    (root / "helper.js").write_text("module.exports = {};\n")
    (root / "mod.js").write_text("export default 1;\n")
    (root / "app.js").write_text(
        "import u from './util';\n"
        "const h = require('./helper');\n"
        "const m = import('./mod');\n"
        "import React from 'react';\n"  # external → not startswith '.' → skipped
    )
    rels = _extract_js_imports(str(root / "app.js"), str(root))
    targets = {r["target"] for r in rels}
    assert targets == {"util.js", "helper.js", "mod.js"}
    assert all(r["source"] == "app.js" and r["type"] == "imports" for r in rels)


def test_extract_js_imports_missing_file_returns_empty(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert _extract_js_imports(str(root / "ghost.js"), str(root)) == []


def test_extract_js_imports_unresolvable_relative_skipped(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    # relative import that points to a nonexistent target → not appended
    (root / "app.js").write_text("import x from './does-not-exist';\n")
    assert _extract_js_imports(str(root / "app.js"), str(root)) == []


# ── _resolve_module_path ─────────────────────────────────────────────


def test_resolve_module_path_module_file(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("")
    assert _resolve_module_path("pkg.mod", str(root)) == os.path.join("pkg", "mod.py")


def test_resolve_module_path_package_init(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    assert _resolve_module_path("pkg", str(root)) == os.path.join("pkg", "__init__.py")


def test_resolve_module_path_none_when_absent(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert _resolve_module_path("does.not.exist", str(root)) is None


# ── _resolve_relative_js ─────────────────────────────────────────────


def test_resolve_relative_js_with_extension(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "util.ts").write_text("")
    from_file = str(root / "app.js")
    assert _resolve_relative_js("./util", from_file, str(root)) == "util.ts"


def test_resolve_relative_js_empty_ext_matches_dir(tmp_path):
    # The first extension candidate is "" which matches an existing directory,
    # so a bare relative dir spec resolves to the directory path itself.
    root = tmp_path / "proj"
    (root / "dir").mkdir(parents=True)
    (root / "dir" / "index.js").write_text("")
    from_file = str(root / "app.js")
    assert _resolve_relative_js("./dir", from_file, str(root)) == "dir"


def test_resolve_relative_js_index_file(tmp_path):
    # No dir/file match until the "/index.ts" candidate is tried.
    root = tmp_path / "proj"
    (root / "widgets").mkdir(parents=True)
    (root / "widgets" / "index.ts").write_text("")
    # spec points at a name whose only match is <name>/index.ts
    from_file = str(root / "app.js")
    assert _resolve_relative_js("./widgets/index", from_file, str(root)) == os.path.join(
        "widgets", "index.ts"
    )


def test_resolve_relative_js_none_when_absent(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    from_file = str(root / "app.js")
    assert _resolve_relative_js("./missing", from_file, str(root)) is None


# ── index_file_relationships ─────────────────────────────────────────


def test_index_file_relationships_summary_and_rows(tmp_path, test_db):
    root = _make_py_project(tmp_path)
    result = index_file_relationships(str(root), db_path=test_db["path"])

    assert result["files_processed"] == 3  # a.py, b.py, c.py
    assert result["added"] == 2  # a->b, a->c
    assert result["skipped"] == 0

    rows = query_relationships(str(root), db_path=test_db["path"])
    pairs = {(r["source_file"], r["target_file"]) for r in rows}
    assert pairs == {("a.py", "b.py"), ("a.py", "c.py")}
    assert all(r["relationship_type"] == "imports" for r in rows)


def test_index_file_relationships_excludes_dirs(tmp_path, test_db):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    # files inside excluded dirs must not be walked
    nm = root / "node_modules"
    nm.mkdir()
    (nm / "lib.js").write_text("import z from './z';\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cached.py").write_text("import a\n")

    result = index_file_relationships(str(root), db_path=test_db["path"])
    assert result["files_processed"] == 1  # only a.py


def test_index_file_relationships_skips_self_reference(tmp_path, test_db):
    root = tmp_path / "proj"
    root.mkdir()
    # a.py imports module 'a' → resolves to a.py itself → source == target → skipped
    (root / "a.py").write_text("import a\n")
    result = index_file_relationships(str(root), db_path=test_db["path"])
    assert result["files_processed"] == 1
    assert result["added"] == 0
    assert query_relationships(str(root), db_path=test_db["path"]) == []


def test_index_file_relationships_clears_stale_on_reindex(tmp_path, test_db):
    root = _make_py_project(tmp_path)
    index_file_relationships(str(root), db_path=test_db["path"])
    # Remove one import target-producing line, re-index
    (root / "a.py").write_text("import b\n")
    result = index_file_relationships(str(root), db_path=test_db["path"])
    assert result["added"] == 1
    rows = query_relationships(str(root), db_path=test_db["path"])
    pairs = {(r["source_file"], r["target_file"]) for r in rows}
    assert pairs == {("a.py", "b.py")}  # stale a->c gone


# ── query_relationships ──────────────────────────────────────────────


def _seed_rels(root, test_db):
    """Directly seed a couple relationships for query-direction tests."""
    from src.database import get_or_create_project

    project = get_or_create_project(str(root), db_path=test_db["path"])
    pid = project["id"]
    with get_connection(test_db["path"]) as conn:
        conn.executemany(
            "INSERT INTO file_relationships (project_id, source_file, target_file, relationship_type)"
            " VALUES (?, ?, ?, ?)",
            [
                (pid, "a.py", "b.py", "imports"),
                (pid, "c.py", "b.py", "imports"),
                (pid, "b.py", "d.py", "imports"),
            ],
        )
    return pid


def test_query_relationships_outgoing(tmp_path, test_db):
    root = tmp_path / "proj"
    root.mkdir()
    _seed_rels(root, test_db)
    rows = query_relationships(str(root), file_path="b.py", direction="outgoing", db_path=test_db["path"])
    pairs = {(r["source_file"], r["target_file"]) for r in rows}
    assert pairs == {("b.py", "d.py")}


def test_query_relationships_incoming(tmp_path, test_db):
    root = tmp_path / "proj"
    root.mkdir()
    _seed_rels(root, test_db)
    rows = query_relationships(str(root), file_path="b.py", direction="incoming", db_path=test_db["path"])
    pairs = {(r["source_file"], r["target_file"]) for r in rows}
    assert pairs == {("a.py", "b.py"), ("c.py", "b.py")}


def test_query_relationships_both(tmp_path, test_db):
    root = tmp_path / "proj"
    root.mkdir()
    _seed_rels(root, test_db)
    rows = query_relationships(str(root), file_path="b.py", direction="both", db_path=test_db["path"])
    pairs = {(r["source_file"], r["target_file"]) for r in rows}
    assert pairs == {("a.py", "b.py"), ("c.py", "b.py"), ("b.py", "d.py")}


def test_query_relationships_all_when_no_file(tmp_path, test_db):
    root = tmp_path / "proj"
    root.mkdir()
    _seed_rels(root, test_db)
    rows = query_relationships(str(root), db_path=test_db["path"])
    assert len(rows) == 3


# ── formatter helpers ────────────────────────────────────────────────


def test_sanitize_mermaid_id():
    assert _sanitize_mermaid_id("src/foo-bar.py") == "src_foo_bar_py"


def test_short_label():
    assert _short_label("src/pkg/mod.py") == "mod.py"


# ── format_mermaid ───────────────────────────────────────────────────


def test_format_mermaid_nodes_and_edges():
    rels = [{"source_file": "src/a.py", "target_file": "src/b.py", "relationship_type": "imports"}]
    out = format_mermaid(rels)
    lines = out.splitlines()
    assert lines[0] == "flowchart LR"
    assert '    src_a_py["a.py"]' in lines
    assert '    src_b_py["b.py"]' in lines
    assert "    src_a_py -->|imports| src_b_py" in lines


def test_format_mermaid_default_relationship_type():
    # missing relationship_type key → defaults to "imports"
    rels = [{"source_file": "a.py", "target_file": "b.py"}]
    out = format_mermaid(rels)
    assert "a_py -->|imports| b_py" in out


def test_format_mermaid_trims_to_max_nodes():
    # hub 'a' connects to b, c, d; with max_nodes=2 only the 2 most-connected survive
    rels = [
        {"source_file": "a", "target_file": "b", "relationship_type": "imports"},
        {"source_file": "a", "target_file": "c", "relationship_type": "imports"},
        {"source_file": "a", "target_file": "d", "relationship_type": "imports"},
    ]
    out = format_mermaid(rels, max_nodes=2)
    node_defs = [ln for ln in out.splitlines() if '["' in ln]
    assert len(node_defs) == 2  # trimmed to 2 unique nodes
    # 'a' is the most connected, must be retained
    assert any('a["' in ln for ln in node_defs)


# ── format_dot ───────────────────────────────────────────────────────


def test_format_dot_structure_and_dedup():
    rels = [
        {"source_file": "a.py", "target_file": "b.py", "relationship_type": "imports"},
        {"source_file": "a.py", "target_file": "c.py", "relationship_type": "imports"},
    ]
    out = format_dot(rels)
    lines = out.splitlines()
    assert lines[0] == "digraph engram_codebase {"
    assert lines[-1] == "}"
    # node 'a.py' emitted exactly once despite two edges
    assert sum(1 for ln in lines if ln.strip() == '"a.py" [label="a.py"];') == 1
    assert '    "a.py" -> "b.py" [label="imports"];' in lines
    assert '    "a.py" -> "c.py" [label="imports"];' in lines


def test_format_dot_default_relationship_type():
    rels = [{"source_file": "a.py", "target_file": "b.py"}]
    out = format_dot(rels)
    assert '"a.py" -> "b.py" [label="imports"];' in out


# ── format_json ──────────────────────────────────────────────────────


def test_format_json_nodes_and_edges():
    rels = [{"source_file": "a.py", "target_file": "b.py", "relationship_type": "imports"}]
    out = format_json(rels)
    parsed = json.loads(out)
    assert set(parsed["nodes"]) == {"a.py", "b.py"}
    assert parsed["edges"] == rels
