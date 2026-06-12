#!/usr/bin/env python3
"""Validate labeled eval query JSON files (structure and ground-truth fields)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_TYPES = frozenset(
    {"mistake", "pattern", "skill", "conversation", "prompt", "fact", "decision", "session"}
)
VALID_CATEGORIES = frozenset(
    {
        "exact_error",
        "semantic_similar",
        "tag_filter",
        "type_inference",
        "multi_hop",
        "conversation",
        "prompt",
        "abstention",
        "user_labeled",
        "public",
    }
)


def validate_query(q: dict, index: int, *, strict_ids: bool) -> list[str]:
    errors: list[str] = []
    prefix = f"queries[{index}]"

    if not isinstance(q, dict):
        return [f"{prefix}: must be an object"]

    qid = q.get("id")
    if not qid or not isinstance(qid, str):
        errors.append(f"{prefix}: missing or invalid 'id' (string required)")

    query_text = q.get("query")
    if not query_text or not isinstance(query_text, str) or not query_text.strip():
        errors.append(f"{prefix} ({qid}): missing or empty 'query'")

    category = q.get("category")
    if category and category not in VALID_CATEGORIES:
        errors.append(f"{prefix} ({qid}): unknown category '{category}'")

    abstention = bool(q.get("expect_abstention"))
    has_type = q.get("expected_type") is not None
    has_id = q.get("expected_item_id") is not None
    has_title = bool(q.get("expected_title_contains"))

    if abstention:
        if has_type or has_id:
            errors.append(
                f"{prefix} ({qid}): abstention queries must not set expected_type/expected_item_id"
            )
    else:
        if has_type and has_id:
            et = q["expected_type"]
            eid = q["expected_item_id"]
            if et not in VALID_TYPES:
                errors.append(f"{prefix} ({qid}): invalid expected_type '{et}'")
            if not isinstance(eid, int) or eid < 1:
                errors.append(f"{prefix} ({qid}): expected_item_id must be positive int")
            elif strict_ids and eid > 10:
                errors.append(
                    f"{prefix} ({qid}): expected_item_id {eid} > 10 (seed range) — "
                    "use --allow-non-seed to permit"
                )
        elif has_type or has_id:
            errors.append(
                f"{prefix} ({qid}): expected_type and expected_item_id must both be set or both absent"
            )
        elif not has_title:
            errors.append(
                f"{prefix} ({qid}): non-abstention query needs "
                "(expected_type + expected_item_id) or expected_title_contains"
            )

    if q.get("deny_items") is not None:
        if not isinstance(q["deny_items"], list):
            errors.append(f"{prefix} ({qid}): deny_items must be a list")
        else:
            for j, d in enumerate(q["deny_items"]):
                if not isinstance(d, dict) or "type" not in d or "item_id" not in d:
                    errors.append(f"{prefix} ({qid}).deny_items[{j}]: need type and item_id")

    ams = q.get("abstention_max_score")
    if ams is not None and (not isinstance(ams, (int, float)) or not 0 <= float(ams) <= 1):
        errors.append(f"{prefix} ({qid}): abstention_max_score must be 0..1")

    return errors


def validate_file(path: Path, *, strict_ids: bool) -> list[str]:
    errors: list[str] = []
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON — {exc}"]

    if not isinstance(data, dict):
        return [f"{path}: root must be an object"]

    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        return [f"{path}: 'queries' must be a non-empty list"]

    seen_ids: set[str] = set()
    for i, q in enumerate(queries):
        for err in validate_query(q, i, strict_ids=strict_ids):
            errors.append(err)
        qid = q.get("id") if isinstance(q, dict) else None
        if isinstance(qid, str):
            if qid in seen_ids:
                errors.append(f"queries[{i}] ({qid}): duplicate id")
            seen_ids.add(qid)

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate eval query JSON labels")
    parser.add_argument(
        "files",
        nargs="*",
        default=[
            "benchmarks/test_queries.json",
            "evals/public_queries.json",
        ],
        help="JSON files to validate (default: test_queries + public_queries)",
    )
    parser.add_argument(
        "--allow-non-seed",
        action="store_true",
        help="Allow expected_item_id > 10 (real-world snapshots)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    strict_ids = not args.allow_non_seed
    all_errors: list[str] = []

    for rel in args.files:
        path = Path(rel)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            all_errors.append(f"{path}: file not found")
            continue
        all_errors.extend(validate_file(path, strict_ids=strict_ids))

    if all_errors:
        print(f"Validation FAILED ({len(all_errors)} issue(s)):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print(f"Validation OK — {len(args.files)} file(s)")


if __name__ == "__main__":
    main()
