"""
Database diagnostics and integrity repair tool.
"""
from .database import get_connection

# Simple formatting helpers so we don't circularly import from cli if we don't have to
def fmt_header(t): return f"\033[1m\033[36m{t}\033[0m"
def fmt_error(t): return f"\033[31m{t}\033[0m"
def fmt_dim(t): return f"\033[2m{t}\033[0m"

def run_diagnostics(repair=False):
    """Scan database for orphans, drift, and structural integrity."""
    issues_found = 0
    issues_fixed = 0
    
    with get_connection() as conn:
        print(fmt_header("Engram Diagnostics\n"))
        
        # 1. Orphaned Tags
        orphans = conn.execute("SELECT id, name FROM tags WHERE id NOT IN (SELECT tag_id FROM item_tags)").fetchall()
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
            print(fmt_error(f"FTS Drift detected: {core_count} core items, but {fts_count} search index entries."))
            if repair:
                print("  ⚠️ Repair: Full FTS rebuild from core tables is required. (Coming soon)")
        else:
            print("✓ FTS Search: Lexical index matches core tables perfectly.")
            
        # 3. Vector Drift
        try:
            vec_count = conn.execute("SELECT COUNT(*) FROM vec_memory").fetchone()[0]
            if fts_count != vec_count:
                issues_found += 1
                print(fmt_error(f"Vector Drift detected: {fts_count} search items, but {vec_count} embeddings."))
                if repair:
                    print("  ⚠️ Repair: Vector rebuild requires re-running embeddings for missing rows. (Coming soon)")
            else:
                print("✓ Vectors: Semantic index matches search index perfectly.")
        except Exception:
            print(fmt_dim("- Vectors: sqlite-vec not active, skipping semantic integrity check."))
            
        print(f"\nDiagnostics complete. {issues_found} issues found. {issues_fixed} issues repaired.")
