#!/usr/bin/env python3
"""Export the indexed Qdrant collection to a flat CSV.

Pipeline: clone_repos.py -> index_qdrant.py -> export_csv.py

Pulls every point's payload straight out of Qdrant (the same source
app/search.py and the Streamlit UI read from), so the CSV always matches
what's actually searchable -- no re-deriving fields from disk.
"""

import argparse
import csv
import re
from pathlib import Path

from qdrant_client import QdrantClient

from index_qdrant import COLLECTION, DB_PATH

OUTPUT_FILE = Path(__file__).parent / "skills_export.csv"
TOP_OUTPUT_FILE = Path(__file__).parent / "skills_export_top.csv"

FIELDS = [
    "name",
    "description",
    "owner",
    "repo",
    "stars",
    "ranking",
    "sources",
    "repo_url",
    "skill_url",
    "path",
    "duplicate_count",
    "also_in",
    "name_collision_count",
    "name_shared_with",
    "content_hash",
]


def best_rank(ranking: str) -> int | None:
    """Lowest (best) value among every `*_rank=N` token in the `ranking`
    string (e.g. "skills_sh_rank=778 search_rank=12" -> 12). None if no
    rank-typed token is present (skill_count/top_installs etc don't count)."""
    values = [int(v) for v in re.findall(r"(?:^|\s)\S*_rank=(\d+)", ranking or "")]
    return min(values) if values else None


def iter_rows(client: QdrantClient):
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION,
            with_payload=True,
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            row = {field: payload.get(field) for field in FIELDS}
            sources = row.get("sources")
            if isinstance(sources, list):
                row["sources"] = ", ".join(sources)
            name_shared_with = row.get("name_shared_with")
            if isinstance(name_shared_with, list):
                row["name_shared_with"] = ", ".join(name_shared_with)
            # "also_in": every other repo/path this exact content also lives
            # at, for reviewing which duplicates got collapsed into this row.
            locations = payload.get("locations") or []
            primary_path = payload.get("path")
            others = [loc for loc in locations if loc.get("path") != primary_path]
            row["also_in"] = "; ".join(f"{loc['owner']}/{loc['repo']}:{loc['path']}" for loc in others)
            yield row
        if offset is None:
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ranked-only", action="store_true",
        help=f"Only include rows with non-empty `ranking` data (skills.sh/search rank, etc). "
        f"Writes to {TOP_OUTPUT_FILE.name} instead of {OUTPUT_FILE.name} unless --output is given.",
    )
    parser.add_argument("--output", type=Path, help="Override the output CSV path.")
    parser.add_argument("--limit", type=int, help="Keep only the first N rows after sorting.")
    args = parser.parse_args()

    client = QdrantClient(path=str(DB_PATH))
    if not client.collection_exists(COLLECTION):
        raise SystemExit(f"Collection {COLLECTION!r} not found at {DB_PATH} -- run index_qdrant.py first")

    rows = list(iter_rows(client))
    if args.ranked_only:
        # best_rank(None) sorts to the end -- only reachable here if a row's
        # ranking string had skill_count/top_installs but no *_rank token.
        for r in rows:
            r["_best_rank"] = best_rank(r.get("ranking"))
        rows = [r for r in rows if r.get("ranking")]
        rows.sort(key=lambda r: (r["_best_rank"] is None, r["_best_rank"] if r["_best_rank"] is not None else 0, (r["name"] or "").lower()))
        for r in rows:
            del r["_best_rank"]
    else:
        rows.sort(key=lambda r: (-(r["stars"] or 0), (r["name"] or "").lower()))

    if args.limit is not None:
        rows = rows[: args.limit]

    output_file = args.output or (TOP_OUTPUT_FILE if args.ranked_only else OUTPUT_FILE)
    with output_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_file}")


if __name__ == "__main__":
    main()
