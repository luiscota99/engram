"""
Database diagnostics and integrity repair tool.
"""

import os
import urllib.error
import urllib.request

from .database import get_connection


# Simple formatting helpers so we don't circularly import from cli if we don't have to
def fmt_header(t):
    return f"\033[1m\033[36m{t}\033[0m"


def fmt_error(t):
    return f"\033[31m{t}\033[0m"


def fmt_dim(t):
    return f"\033[2m{t}\033[0m"


def run_diagnostics(repair=False):
    """Scan database for orphans, drift, and structural integrity."""
    issues_found = 0
    issues_fixed = 0

    with get_connection() as conn:
        print(fmt_header("Engram Diagnostics\n"))

        # 1. Orphaned Tags
        orphans = conn.execute(
            "SELECT id, name FROM tags WHERE id NOT IN (SELECT tag_id FROM item_tags)"
        ).fetchall()
        if orphans:
            issues_found += len(orphans)
            print(fmt_error(f"Found {len(orphans)} orphaned tags (not linked to any memory)."))
            if repair:
                ids = [str(r["id"]) for r in orphans]
                conn.execute(f"DELETE FROM tags WHERE id IN ({','.join(ids)})")
                issues_fixed += len(orphans)
                print("  ✓ Repair: Deleted orphaned tags.")
        else:
            print("✓ Tags: No orphaned tags found.")

        # 2. FTS Drift
        core_count = 0
        for table in ["mistakes", "patterns", "skills", "conversations", "prompts"]:
            core_count += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        fts_count = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        if core_count != fts_count:
            issues_found += 1
            print(
                fmt_error(
                    f"FTS Drift detected: {core_count} core items, but {fts_count} search index entries."
                )
            )
            if repair:
                print("  ⚠️ Repair: Full FTS rebuild from core tables is required. (Coming soon)")
        else:
            print("✓ FTS Search: Lexical index matches core tables perfectly.")

        # 3. Vector Drift
        try:
            vec_count = conn.execute("SELECT COUNT(*) FROM vec_memory").fetchone()[0]
            if fts_count != vec_count:
                issues_found += 1
                print(
                    fmt_error(
                        f"Vector Drift detected: {fts_count} search items, but {vec_count} embeddings."
                    )
                )
                if repair:
                    print(fmt_dim("  Running Vector Repair: Generating missing embeddings..."))
                    rows = conn.execute(
                        "SELECT rowid, title, content, tags FROM memory_fts"
                    ).fetchall()
                    import json

                    from .embeddings import embed_text

                    fixed = 0
                    for r in rows:
                        has_vec = conn.execute(
                            "SELECT rowid FROM vec_memory WHERE rowid = ?", (r["rowid"],)
                        ).fetchone()
                        if not has_vec:
                            full_text = f"{r['title']}\n{r['content']}\n{r['tags']}"
                            emb = embed_text(full_text)
                            if emb:
                                conn.execute(
                                    "INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                                    (r["rowid"], json.dumps(emb)),
                                )
                                fixed += 1
                    issues_fixed += 1
                    print(f"  ✓ Repair: Generated {fixed} missing embeddings.")
            else:
                print("✓ Vectors: Semantic index matches search index perfectly.")
        except Exception:
            print(fmt_dim("- Vectors: sqlite-vec not active, skipping semantic integrity check."))

        # 4. Semantic Engine Health (Ollama)
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            req = urllib.request.Request(ollama_host, method="GET")
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200:
                    print("✓ Semantic Engine: Ollama is reachable.")
                else:
                    issues_found += 1
                    print(
                        fmt_error(
                            f"Semantic Engine Error: Ollama returned status {response.status}."
                        )
                    )
        except urllib.error.URLError as e:
            issues_found += 1
            print(
                fmt_error(
                    f"Semantic Engine Offline: Could not connect to Ollama at {ollama_host}. Vector search will fail silently! ({e.reason})"
                )
            )
        except Exception as e:
            issues_found += 1
            print(fmt_error(f"Semantic Engine Offline: {str(e)}"))

        # 5. Dynamic Index Suggestions (Anti-Bloat/Performance)
        for table in ["mistakes", "patterns", "skills", "conversations", "prompts"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > 10000:
                print(
                    fmt_error(
                        f"Performance Warning: The `{table}` table has >10,000 rows ({count})."
                    )
                )
                print(
                    fmt_dim(
                        f"  → Consider adding a covering index (e.g., `CREATE INDEX idx_{table}_domain_date ON {table}(domain, date)`) to maintain fast query speeds."
                    )
                )

        print(
            f"\nDiagnostics complete. {issues_found} issues found. {issues_fixed} issues repaired."
        )
