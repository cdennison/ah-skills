#!/usr/bin/env python3
"""Flatten npm_mcp_candidates.json into a CSV for easy review in a
spreadsheet -- one row per candidate, all fields gathered so far
(fetch_npm_mcp_candidates.py + backfill_readmes.py + classify_mcp.py).

Includes "mcp_category_source" (rule vs manual) and "claude_opinion" --
classify_mcp.py already applies manual_classifications.py's overrides to
mcp_category itself and stamps claude_opinion onto the entry, so this
script just passes both through; it doesn't duplicate the lookup table.

Usage:
    python export_csv.py
"""

import csv
import json
from pathlib import Path

DATA_PATH = Path(__file__).parent / "npm_mcp_candidates.json"
OUTPUT_PATH = Path(__file__).parent / "npm_mcp_candidates.csv"

# Columns, in output order. Some are derived/serialized below.
FIELDS = [
    "name",
    "description",
    "version",
    "mcp_category",
    "mcp_category_source",
    "claude_opinion",
    "has_mcp_sdk_dependency",
    "mcp_category_signals",
    "license",
    "homepage",
    "repository_url",
    "package_url",
    "author",
    "maintainers",
    "keywords",
    "engines",
    "dependencies",
    "peer_dependencies",
    "bin",
    "monthly_downloads",
    "search_score",
    "created",
    "modified",
    "readme_source",
    "readme",
]


def repo_url(entry: dict) -> str:
    repo = entry.get("repository")
    if isinstance(repo, dict):
        return repo.get("url") or ""
    if isinstance(repo, str):
        return repo
    return ""


def npm_package_url(entry: dict) -> str:
    return f"https://www.npmjs.com/package/{entry['name']}"


def to_row(entry: dict) -> dict:
    row = {field: entry.get(field, "") for field in FIELDS}
    row["repository_url"] = repo_url(entry)
    row["package_url"] = npm_package_url(entry)

    # Serialize anything non-scalar to compact JSON so it round-trips cleanly
    # in a single CSV cell.
    for field in ("author", "maintainers", "keywords", "engines", "dependencies", "peer_dependencies", "bin", "mcp_category_signals"):
        value = row.get(field)
        row[field] = json.dumps(value, ensure_ascii=False) if value not in (None, "", {}) else ""

    return row


def main():
    entries = json.loads(DATA_PATH.read_text())

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(to_row(entry))

    still_unclear = [e["name"] for e in entries if e.get("mcp_category") == "unclear"]
    manual = sum(1 for e in entries if e.get("mcp_category_source") == "manual")
    print(f"wrote {len(entries)} rows to {OUTPUT_PATH}")
    print(f"{manual} row(s) carry a manual classification override")
    if still_unclear:
        print(f"still unclear, no manual override on file: {still_unclear}")


if __name__ == "__main__":
    main()
