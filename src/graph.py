"""
Graph module — cross-file relationship mapping and visualization.

Parses source file imports/requires to build a dependency graph stored in
the file_relationships table.  Supports output in Mermaid, DOT, and JSON formats.
"""

from __future__ import annotations


import ast
import os
import re

from .database import get_connection, get_or_create_project


# ── Relationship Extraction ──────────────────────────────────────────


def _extract_python_imports(file_path: str, project_root: str) -> list[dict]:
    """Parse a Python file and return internal import relationships."""
    relationships = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return relationships

    rel_source = os.path.relpath(file_path, project_root)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_path = _resolve_module_path(alias.name, project_root)
                if module_path:
                    relationships.append({
                        "source": rel_source,
                        "target": module_path,
                        "type": "imports",
                    })
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_path = _resolve_module_path(node.module, project_root, file_path)
                if module_path:
                    relationships.append({
                        "source": rel_source,
                        "target": module_path,
                        "type": "imports",
                    })

    return relationships


def _extract_js_imports(file_path: str, project_root: str) -> list[dict]:
    """Parse a JS/TS file and return internal import relationships."""
    relationships = []
    rel_source = os.path.relpath(file_path, project_root)
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return relationships

    # Match: import ... from './foo', require('./foo'), import('./foo')
    pattern = re.compile(
        r"""(?:import\s.*?from\s+|require\s*\(\s*|import\s*\(\s*)['"`]([^'"`]+)['"`]""",
        re.DOTALL,
    )
    for match in pattern.finditer(content):
        spec = match.group(1)
        if spec.startswith("."):
            target = _resolve_relative_js(spec, file_path, project_root)
            if target:
                relationships.append({"source": rel_source, "target": target, "type": "imports"})

    return relationships


def _resolve_module_path(module: str, project_root: str, from_file: str | None = None) -> str | None:
    """Try to resolve a Python module name to a relative file path."""
    # Convert dotted module to path
    parts = module.split(".")
    candidates = [
        os.path.join(*parts) + ".py",
        os.path.join(*parts, "__init__.py"),
    ]
    for cand in candidates:
        full = os.path.join(project_root, cand)
        if os.path.exists(full):
            return cand
    return None


def _resolve_relative_js(spec: str, from_file: str, project_root: str) -> str | None:
    """Resolve a relative JS import path to a project-relative path."""
    base_dir = os.path.dirname(from_file)
    resolved = os.path.normpath(os.path.join(base_dir, spec))

    extensions = ["", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts"]
    for ext in extensions:
        candidate = resolved + ext
        if os.path.exists(candidate):
            return os.path.relpath(candidate, project_root)
    return None


def index_file_relationships(project_path: str, db_path=None) -> dict:
    """Walk a project and build the file_relationships table.

    Returns a summary dict: {added, skipped, files_processed}.
    """
    project_path = os.path.abspath(project_path)
    project = get_or_create_project(project_path, db_path=db_path)
    project_id = project["id"]

    exclude_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".engram", "dist", "build"}
    supported_ext = {".py", ".js", ".ts", ".jsx", ".tsx"}

    all_relationships = []
    files_processed = 0

    for root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fname in filenames:
            _, ext = os.path.splitext(fname)
            if ext not in supported_ext:
                continue
            fp = os.path.join(root, fname)
            files_processed += 1
            if ext == ".py":
                all_relationships.extend(_extract_python_imports(fp, project_path))
            elif ext in {".js", ".ts", ".jsx", ".tsx"}:
                all_relationships.extend(_extract_js_imports(fp, project_path))

    added = skipped = 0
    with get_connection(db_path) as conn:
        # Clear stale entries for this project
        conn.execute("DELETE FROM file_relationships WHERE project_id = ?", (project_id,))
        for rel in all_relationships:
            if rel["source"] == rel["target"]:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO file_relationships
                       (project_id, source_file, target_file, relationship_type)
                       VALUES (?, ?, ?, ?)""",
                    (project_id, rel["source"], rel["target"], rel["type"]),
                )
                added += 1
            except Exception:
                skipped += 1

    return {"added": added, "skipped": skipped, "files_processed": files_processed}


def query_relationships(
    project_path: str,
    file_path: str | None = None,
    direction: str = "both",
    db_path=None,
) -> list[dict]:
    """Query file relationships for a project.

    direction: 'outgoing' (imports), 'incoming' (imported-by), 'both'
    """
    project = get_or_create_project(project_path, db_path=db_path)
    project_id = project["id"]

    with get_connection(db_path) as conn:
        if file_path is None:
            rows = conn.execute(
                "SELECT source_file, target_file, relationship_type FROM file_relationships "
                "WHERE project_id = ? ORDER BY source_file, target_file",
                (project_id,),
            ).fetchall()
        elif direction == "outgoing":
            rows = conn.execute(
                "SELECT source_file, target_file, relationship_type FROM file_relationships "
                "WHERE project_id = ? AND source_file = ?",
                (project_id, file_path),
            ).fetchall()
        elif direction == "incoming":
            rows = conn.execute(
                "SELECT source_file, target_file, relationship_type FROM file_relationships "
                "WHERE project_id = ? AND target_file = ?",
                (project_id, file_path),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_file, target_file, relationship_type FROM file_relationships "
                "WHERE project_id = ? AND (source_file = ? OR target_file = ?)",
                (project_id, file_path, file_path),
            ).fetchall()

    return [dict(r) for r in rows]


# ── Output Formatters ────────────────────────────────────────────────


def _sanitize_mermaid_id(path: str) -> str:
    """Convert a file path into a valid Mermaid node ID."""
    return path.replace("/", "_").replace(".", "_").replace("-", "_")


def _short_label(path: str) -> str:
    """Use just the filename as the label (full path as tooltip)."""
    return os.path.basename(path)


def format_mermaid(relationships: list[dict], max_nodes: int = 50) -> str:
    """Render relationships as a Mermaid flowchart LR diagram.

    Nodes are labelled with filename; edges represent relationship_type.
    Limits to max_nodes unique files to keep diagrams readable.
    """
    all_files: set[str] = set()
    for r in relationships:
        all_files.add(r["source_file"])
        all_files.add(r["target_file"])

    # If too many nodes, trim to the most-connected ones
    if len(all_files) > max_nodes:
        from collections import Counter
        counts: Counter = Counter()
        for r in relationships:
            counts[r["source_file"]] += 1
            counts[r["target_file"]] += 1
        top_files = {f for f, _ in counts.most_common(max_nodes)}
        relationships = [
            r for r in relationships
            if r["source_file"] in top_files and r["target_file"] in top_files
        ]
        all_files = top_files

    lines = ["flowchart LR"]

    # Emit node definitions with short labels
    for f in sorted(all_files):
        nid = _sanitize_mermaid_id(f)
        label = _short_label(f)
        lines.append(f'    {nid}["{label}"]')

    lines.append("")

    # Emit edges
    for r in relationships:
        src = _sanitize_mermaid_id(r["source_file"])
        tgt = _sanitize_mermaid_id(r["target_file"])
        rtype = r.get("relationship_type", "imports")
        lines.append(f"    {src} -->|{rtype}| {tgt}")

    return "\n".join(lines)


def format_dot(relationships: list[dict]) -> str:
    """Render relationships as a Graphviz DOT digraph."""
    lines = ['digraph engram_codebase {', '    rankdir=LR;', '    node [shape=box];']
    emitted_nodes: set[str] = set()
    for r in relationships:
        for fname in (r["source_file"], r["target_file"]):
            if fname not in emitted_nodes:
                label = _short_label(fname)
                lines.append(f'    "{fname}" [label="{label}"];')
                emitted_nodes.add(fname)
        rtype = r.get("relationship_type", "imports")
        lines.append(f'    "{r["source_file"]}" -> "{r["target_file"]}" [label="{rtype}"];')
    lines.append("}")
    return "\n".join(lines)


def format_json(relationships: list[dict]) -> str:
    import json
    nodes = list({r["source_file"] for r in relationships} | {r["target_file"] for r in relationships})
    return json.dumps({"nodes": nodes, "edges": relationships}, indent=2)
