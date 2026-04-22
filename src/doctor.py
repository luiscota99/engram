"""
Database diagnostics and integrity repair tool.
"""

import os
import urllib.error
import urllib.request

from .database import get_connection, rebuild_fts


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
        for table in ["mistakes", "patterns", "skills", "conversations", "prompts", "sessions"]:
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
                print(fmt_dim("  Running FTS Rebuild from core tables..."))
                rebuild_fts(conn)
                issues_fixed += 1
                print("  ✓ Repair: FTS index rebuilt successfully. All core items are now indexed.")
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
        ollama_available = False
        try:
            req = urllib.request.Request(ollama_host, method="GET")
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200:
                    ollama_available = True
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

        # 5. Failed Embeddings Recovery
        failed_count = conn.execute(
            "SELECT COUNT(*) FROM embedding_status WHERE status = 'failed'"
        ).fetchone()[0]
        if failed_count > 0:
            issues_found += 1
            print(fmt_error(f"Found {failed_count} failed embeddings that can be retried."))
            if repair and ollama_available:
                conn.execute(
                    "UPDATE embedding_status SET status = 'pending', error_message = NULL, "
                    "updated_at = datetime('now') WHERE status = 'failed'"
                )
                issues_fixed += 1
                print(f"  ✓ Repair: Reset {failed_count} failed embeddings to pending.")
                print(fmt_dim("    Run `engram reembed` to regenerate them."))
            elif repair:
                print(fmt_dim("  Skipping reset: Ollama is not available. Start Ollama first, then re-run doctor --repair."))
        else:
            print("✓ Embeddings: No failed embeddings found.")

        # 6. Orphaned embedding_status entries
        orphan_status = conn.execute(
            "SELECT COUNT(*) FROM embedding_status es "
            "LEFT JOIN memory_fts mf ON mf.rowid = es.fts_rowid "
            "WHERE mf.rowid IS NULL"
        ).fetchone()[0]
        if orphan_status > 0:
            issues_found += 1
            print(fmt_error(f"Found {orphan_status} orphaned embedding_status entries."))
            if repair:
                conn.execute(
                    "DELETE FROM embedding_status WHERE fts_rowid NOT IN (SELECT rowid FROM memory_fts)"
                )
                issues_fixed += 1
                print(f"  ✓ Repair: Removed {orphan_status} orphaned embedding_status entries.")
        else:
            print("✓ Embedding Status: No orphaned entries found.")

        # 7. Dynamic Index Suggestions (Anti-Bloat/Performance)
        for table in ["mistakes", "patterns", "skills", "conversations", "prompts", "sessions"]:
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
