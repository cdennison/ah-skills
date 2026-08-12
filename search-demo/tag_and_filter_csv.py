#!/usr/bin/env python3
"""Tag every row of a skills CSV with agent_target metadata and drop rows
that target OpenClaw/Hermes or live inside a junk aggregator repo (detected
either structurally -- a dump-dir path pattern -- or statistically -- most
of the repo's content cross-owner-duplicates another repo; see
aggregator_filter.py).

Usage:
    python3 tag_and_filter_csv.py skills_export_top.csv

Writes <file>.bak (untouched original) and overwrites <file> in place with:
  - all original columns, unchanged
  - agent_target            (semicolon-joined agent_targets list)
  - agent_target_confidence (high | medium | low)
  - agent_target_evidence   (semicolon-joined evidence strings)
  - aggregator_flag         (true/false)
  - aggregator_evidence

Rows dropped from the output:
  - agent_target includes "openclaw" or "hermes"
  - aggregator_flag is true

Nothing under search-raw/ or in Qdrant is touched by this script.
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

from agent_target import classify_from_metadata
from aggregator_filter import find_statistical_aggregators, is_aggregator_row

EXCLUDED_TARGETS = {"openclaw", "hermes"}

NEW_FIELDS = [
    "agent_target",
    "agent_target_confidence",
    "agent_target_evidence",
    "aggregator_flag",
    "aggregator_evidence",
]


def process(csv_path: Path) -> None:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    print(f"[{csv_path.name}] read {len(rows)} rows")

    stat_aggregators = find_statistical_aggregators(rows)
    print(f"[{csv_path.name}] {len(stat_aggregators)} repos flagged as cross-owner aggregators")
    for (owner, repo), evidence in sorted(stat_aggregators.items()):
        print(f"    {owner}/{repo}: {evidence}")

    tagged_rows = []
    excluded_agent = 0
    excluded_aggregator = 0
    target_counts: dict[str, int] = {}

    for row in rows:
        result = classify_from_metadata(
            path=row.get("path", ""),
            name=row.get("name", ""),
            description=row.get("description", ""),
            owner=row.get("owner", ""),
            repo=row.get("repo", ""),
        )
        agg_flagged, agg_evidence = is_aggregator_row(row, stat_aggregators)

        row["agent_target"] = ";".join(result["agent_targets"])
        row["agent_target_confidence"] = result["confidence"]
        row["agent_target_evidence"] = ";".join(result["evidence"])
        row["aggregator_flag"] = "true" if agg_flagged else "false"
        row["aggregator_evidence"] = agg_evidence

        for t in result["agent_targets"]:
            target_counts[t] = target_counts.get(t, 0) + 1

        if agg_flagged:
            excluded_aggregator += 1
            continue
        if EXCLUDED_TARGETS & set(result["agent_targets"]):
            excluded_agent += 1
            continue

        tagged_rows.append(row)

    print(f"[{csv_path.name}] agent_target distribution (pre-filter):")
    for t, c in sorted(target_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {t}: {c}")

    print(
        f"[{csv_path.name}] excluding {excluded_agent} openclaw/hermes rows, "
        f"{excluded_aggregator} aggregator rows -> {len(tagged_rows)} rows kept"
    )

    backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(csv_path, backup_path)
        print(f"[{csv_path.name}] backup written to {backup_path.name}")
    else:
        print(f"[{csv_path.name}] backup already exists at {backup_path.name}, not overwriting")

    out_fieldnames = list(fieldnames) + NEW_FIELDS
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(tagged_rows)

    print(f"[{csv_path.name}] wrote {len(tagged_rows)} rows\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: tag_and_filter_csv.py <csv-file> [<csv-file> ...]", file=sys.stderr)
        sys.exit(1)
    for arg in sys.argv[1:]:
        process(Path(arg))
