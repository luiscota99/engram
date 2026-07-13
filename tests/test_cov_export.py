"""Coverage tests for src/export.py — SKILL.md rendering, export, import, sync diff."""
import os

from src.database import get_connection
from src.export import (
    classify_domain_from_skill,
    compute_sync_diff,
    content_hash,
    export_skills,
    extract_when_to_use,
    import_cursor_skill,
    import_cursor_skills_dir,
    parse_skill_md,
    render_pattern_as_skill_md,
    render_skill_md,
    slugify,
    write_skill_file,
)

# ── slugify ───────────────────────────────────────────────────────────


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Ship It Fast!") == "ship-it-fast"


def test_slugify_strips_leading_trailing_hyphens_and_truncates():
    assert slugify("  --Hello World--  ") == "hello-world"
    # 100 'a's collapse to a single 64-char run (no separators)
    assert slugify("a" * 100) == "a" * 64


# ── render_skill_md ─────────────────────────────────────────────────────


def test_render_skill_md_full_fields():
    skill = {
        "id": 7,
        "name": "Deploy To Prod",
        "domain": "process",
        "trigger_desc": "when shipping a release",
        "workflow": "1. build\n2. deploy",
        "pitfalls": "forgetting migrations",
        "key_files": "deploy.sh",
        "dependencies": "docker",
        "usage_count": 5,
    }
    md = render_skill_md(skill, tags=["ops", "release"])
    assert "name: deploy-to-prod" in md
    assert "# Deploy To Prod" in md
    assert "1. build" in md
    assert "## When to Use\nwhen shipping a release" in md
    assert "## Common Pitfalls\nforgetting migrations" in md
    assert "## Key Files\ndeploy.sh" in md
    assert "## Dependencies\ndocker" in md
    assert "- Engram ID: 7" in md
    assert "- Domain: process" in md
    assert "- Usage count: 5" in md
    assert "- Tags: ops, release" in md
    # description line carries trigger + domain + tags
    assert "when shipping a release Domain: process. Tags: ops, release." in md


def test_render_skill_md_minimal_defaults():
    # Only a name; every optional field absent → defaults kick in.
    md = render_skill_md({"name": "Bare"})
    assert "name: bare" in md
    assert "- Domain: engineering" in md
    assert "- Usage count: 0" in md
    assert "- Engram ID: unknown" in md
    # No optional sections rendered
    assert "## When to Use" not in md
    assert "## Common Pitfalls" not in md
    assert "## Key Files" not in md
    assert "## Dependencies" not in md
    # No tags line
    assert "- Tags:" not in md


def test_render_skill_md_domain_already_in_trigger_not_duplicated():
    md = render_skill_md(
        {"name": "X", "trigger_desc": "use in backend work", "domain": "backend"}
    )
    # domain "backend" already appears in trigger → no "Domain: backend." in desc
    desc_line = [line for line in md.splitlines() if line.strip().startswith("use in backend")][0]
    assert "Domain: backend." not in desc_line


# ── render_pattern_as_skill_md ──────────────────────────────────────────


def test_render_pattern_as_skill_md():
    pattern = {
        "id": 3,
        "name": "N Plus One",
        "symptoms": "slow query loops",
        "root_cause": "lazy loading in a loop",
        "standard_fix": "eager load",
        "occurrences": 4,
    }
    md = render_pattern_as_skill_md(pattern, tags=["db", "perf"])
    assert "name: n-plus-one" in md
    assert "# N Plus One (Pattern)" in md
    assert "## Symptoms\nslow query loops" in md
    assert "## Root Cause\nlazy loading in a loop" in md
    assert "## Standard Fix\neager load" in md
    assert "- Engram Pattern ID: 3" in md
    assert "- Occurrences: 4" in md
    assert "- Tags: db, perf" in md
    assert "Recognise and fix the 'N Plus One' pattern. slow query loops" in md


def test_render_pattern_as_skill_md_no_tags_defaults():
    md = render_pattern_as_skill_md({"name": "Bare Pattern"})
    assert "- Engram Pattern ID: unknown" in md
    assert "- Occurrences: 0" in md
    assert "- Tags:" not in md


# ── write_skill_file / content_hash ─────────────────────────────────────


def test_write_skill_file_creates_nested_path(tmp_path):
    path = write_skill_file(str(tmp_path), "my-slug", "hello world")
    assert path == os.path.join(str(tmp_path), "my-slug", "SKILL.md")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert f.read() == "hello world"


def test_content_hash_deterministic_16_hex():
    h = content_hash("abc")
    assert len(h) == 16
    assert h == content_hash("abc")
    assert h != content_hash("abd")


# ── export_skills ───────────────────────────────────────────────────────


def _seed_skill(path, name, domain="backend", usage=0):
    with get_connection(path) as conn:
        cur = conn.execute(
            "INSERT INTO skills (name, domain, trigger_desc, workflow, usage_count) "
            "VALUES (?, ?, 'trig', 'flow', ?)",
            (name, domain, usage),
        )
        return cur.lastrowid


def test_export_skills_creates_files(test_db, tmp_path):
    _seed_skill(test_db["path"], "Alpha Skill", usage=3)
    out = str(tmp_path / "out")
    results = export_skills(out)
    assert len(results) == 1
    r = results[0]
    assert r["slug"] == "alpha-skill"
    assert r["action"] == "created"
    assert r["usage_count"] == 3
    assert os.path.isfile(r["path"])


def test_export_skills_dry_run_writes_nothing(test_db, tmp_path):
    _seed_skill(test_db["path"], "Beta Skill")
    out = str(tmp_path / "out")
    results = export_skills(out, dry_run=True)
    assert results[0]["action"] == "dry-run"
    assert not os.path.exists(results[0]["path"])


def test_export_skills_skips_existing(test_db, tmp_path):
    _seed_skill(test_db["path"], "Gamma Skill")
    out = str(tmp_path / "out")
    export_skills(out)  # first pass creates
    results = export_skills(out)  # second pass skips
    assert results[0]["action"] == "skipped"


def test_export_skills_filters_by_ids_domain_min_usage(test_db, tmp_path):
    sid_a = _seed_skill(test_db["path"], "Front One", domain="frontend", usage=10)
    _seed_skill(test_db["path"], "Back One", domain="backend", usage=1)
    out = str(tmp_path / "out")

    # ids filter
    res_ids = export_skills(out, ids=[sid_a], dry_run=True)
    assert [r["name"] for r in res_ids] == ["Front One"]

    # domain filter
    res_dom = export_skills(out, domain="backend", dry_run=True)
    assert [r["name"] for r in res_dom] == ["Back One"]

    # min_usage filter
    res_min = export_skills(out, min_usage=5, dry_run=True)
    assert [r["name"] for r in res_min] == ["Front One"]


def test_export_skills_from_patterns(test_db, tmp_path):
    with get_connection(test_db["path"]) as conn:
        cur = conn.execute(
            "INSERT INTO patterns (name, symptoms, root_cause, standard_fix) "
            "VALUES ('Flaky Test', 'random fail', 'timing', 'add wait')"
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO pattern_occurrences (pattern_id, notes) VALUES (?, 'seen')",
            (pid,),
        )
    out = str(tmp_path / "out")
    results = export_skills(out, from_patterns=True)
    pat = [r for r in results if r.get("source") == "pattern"]
    assert len(pat) == 1
    assert pat[0]["slug"] == "flaky-test"
    assert pat[0]["occurrences"] == 1
    assert pat[0]["action"] == "created"
    assert os.path.isfile(pat[0]["path"])


# ── parse_skill_md ──────────────────────────────────────────────────────


def test_parse_skill_md_block_description(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: my-skill\ndescription: >-\n  Line one.\n  Line two.\n---\n"
        "# Body\nHello.\n",
        encoding="utf-8",
    )
    parsed = parse_skill_md(str(p))
    assert parsed["name"] == "my-skill"
    assert parsed["description"] == "Line one. Line two."
    assert parsed["body"] == "# Body\nHello."
    assert parsed["file_path"] == str(p)


def test_parse_skill_md_inline_description(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: inline\ndescription: A short desc\n---\nbody text\n",
        encoding="utf-8",
    )
    parsed = parse_skill_md(str(p))
    assert parsed["name"] == "inline"
    assert parsed["description"] == "A short desc"


def test_parse_skill_md_missing_file_returns_none(tmp_path):
    assert parse_skill_md(str(tmp_path / "nope.md")) is None


def test_parse_skill_md_no_frontmatter_returns_none(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("just body, no frontmatter\n", encoding="utf-8")
    assert parse_skill_md(str(p)) is None


def test_parse_skill_md_no_name_returns_none(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\ndescription: only desc\n---\nbody\n", encoding="utf-8")
    assert parse_skill_md(str(p)) is None


def test_parse_skill_md_no_description_field(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: only-name\n---\nbody\n", encoding="utf-8")
    parsed = parse_skill_md(str(p))
    assert parsed["name"] == "only-name"
    assert parsed["description"] == ""


# ── classify_domain_from_skill ──────────────────────────────────────────


def test_classify_domain_all_branches():
    assert classify_domain_from_skill("React UI", "") == "frontend"
    assert classify_domain_from_skill("Node API server", "") == "backend"
    assert classify_domain_from_skill("Security review", "vulnerability") == "security"
    assert classify_domain_from_skill("pytest coverage", "tdd") == "testing"
    assert classify_domain_from_skill("Debug incident", "error") == "debugging"
    assert classify_domain_from_skill("Git ship branch", "") == "process"
    assert classify_domain_from_skill("Brainstorm the plan", "") == "planning"
    assert classify_domain_from_skill("Engram memory agent", "") == "tooling"
    assert classify_domain_from_skill("Something else", "misc") == "engineering"


# ── extract_when_to_use ─────────────────────────────────────────────────


def test_extract_when_to_use_from_section():
    body = "# Title\n\n## When to Use\nUse when deploying.\n\n## Other\nignore me"
    assert extract_when_to_use(body, "fallback") == "Use when deploying."


def test_extract_when_to_use_falls_back_to_description():
    assert extract_when_to_use("no section here", "the description") == "the description"


# ── import_cursor_skill ─────────────────────────────────────────────────


def _write_skill(dir_path, slug, name, body="## When to Use\nWhen X happens.\n\nDetails."):
    skill_dir = os.path.join(dir_path, slug)
    os.makedirs(skill_dir, exist_ok=True)
    p = os.path.join(skill_dir, "SKILL.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: A test skill\n---\n{body}\n")
    return p


def test_import_cursor_skill_imports_and_persists(test_db, tmp_path):
    # Trailing "## Details" section makes the When-to-Use extraction stop cleanly.
    p = _write_skill(
        str(tmp_path),
        "my-skill",
        "Testing Helper",
        body="## When to Use\nWhen X happens.\n\n## Details\nmore",
    )
    with get_connection(test_db["path"]) as conn:
        result = import_cursor_skill(p, conn)
    assert result["action"] == "imported"
    assert result["name"] == "Testing Helper"
    # Row persisted with classified domain + extracted trigger
    with get_connection(test_db["path"]) as conn:
        row = conn.execute(
            "SELECT domain, trigger_desc, dependencies FROM skills WHERE name = ?",
            ("Testing Helper",),
        ).fetchone()
    assert row["domain"] == "testing"
    assert row["trigger_desc"] == "When X happens."
    assert row["dependencies"] == "cursor-skill"


def test_import_cursor_skill_skips_existing(test_db, tmp_path):
    _seed_skill(test_db["path"], "Dup Skill")
    p = _write_skill(str(tmp_path), "dup", "Dup Skill")
    with get_connection(test_db["path"]) as conn:
        result = import_cursor_skill(p, conn)
    assert result["action"] == "skipped"
    assert result["reason"] == "already_exists"


def test_import_cursor_skill_dry_run(test_db, tmp_path):
    p = _write_skill(str(tmp_path), "dry", "Dry Skill")
    with get_connection(test_db["path"]) as conn:
        result = import_cursor_skill(p, conn, dry_run=True)
        assert result["action"] == "dry-run"
        # nothing inserted
        assert conn.execute(
            "SELECT COUNT(*) c FROM skills WHERE name = 'Dry Skill'"
        ).fetchone()["c"] == 0


def test_import_cursor_skill_parse_error(test_db, tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("no frontmatter at all", encoding="utf-8")
    with get_connection(test_db["path"]) as conn:
        result = import_cursor_skill(str(bad), conn)
    assert result["action"] == "error"
    assert result["reason"] == "parse_failed"


# ── import_cursor_skills_dir ────────────────────────────────────────────


def test_import_cursor_skills_dir_not_found(test_db, tmp_path):
    results = import_cursor_skills_dir(str(tmp_path / "missing"))
    assert results[0]["error"].startswith("Directory not found:")


def test_import_cursor_skills_dir_walks_and_skips_cursor_dir(test_db, tmp_path):
    base = tmp_path / "skills"
    base.mkdir()
    _write_skill(str(base), "one", "Skill One")
    _write_skill(str(base), "two", "Skill Two")
    # A skills-cursor directory that must be ignored
    _write_skill(str(base), "skills-cursor", "Ignored Skill")

    results = import_cursor_skills_dir(str(base))
    names = sorted(r.get("name") for r in results if "name" in r)
    assert names == ["Skill One", "Skill Two"]
    assert all(r["action"] == "imported" for r in results)


# ── compute_sync_diff ───────────────────────────────────────────────────


def test_compute_sync_diff_partitions(test_db, tmp_path):
    # Engram has "Only Engram" and "Shared Skill"
    _seed_skill(test_db["path"], "Only Engram")
    _seed_skill(test_db["path"], "Shared Skill")

    # Cursor dir has "shared-skill" and "only-cursor"
    cdir = tmp_path / "cursor"
    cdir.mkdir()
    _write_skill(str(cdir), "shared-skill", "Shared Skill")
    _write_skill(str(cdir), "only-cursor", "Only Cursor")

    diff = compute_sync_diff(str(cdir))
    assert "only-engram" in diff["only_in_engram"]
    assert "shared-skill" not in diff["only_in_engram"]
    assert "only-cursor" in diff["only_in_cursor"]
    assert "shared-skill" in diff["in_both"]
    assert diff["in_both"]["shared-skill"]["engram"]["name"] == "Shared Skill"
    assert diff["cursor_dir"] == str(cdir)


def test_compute_sync_diff_missing_dir(test_db, tmp_path):
    _seed_skill(test_db["path"], "Lonely Skill")
    diff = compute_sync_diff(str(tmp_path / "nonexistent"))
    assert "lonely-skill" in diff["only_in_engram"]
    assert diff["only_in_cursor"] == {}
    assert diff["in_both"] == {}
