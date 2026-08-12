#!/usr/bin/env python3
"""Compare /search-raw on disk vs what's actually indexed in Qdrant.

Run any time to sanity-check that the last index_qdrant.py run is caught up.
"""

import csv
import datetime
from pathlib import Path

from export_csv import OUTPUT_FILE as CSV_FILE
from index_qdrant import COLLECTION, SEARCH_RAW_DIR, get_client, load_skills
from registry import load_registry


def print_registry_stats():
    registry = load_registry()
    today = datetime.date.today().isoformat()

    active = [r for r in registry if r.get("status", "active") == "active"]
    skipped = [r for r in registry if r.get("status", "active") != "active"]

    synced_today = [r for r in active if (r.get("last_synced") or "").startswith(today)]
    never_synced = [r for r in active if not r.get("last_synced")]
    stale = [r for r in active if r.get("last_synced") and not r["last_synced"].startswith(today)]
    failed = [r for r in active if r.get("last_sync_failure")]

    print("--- registry (repo-seeds/registry.json) ---")
    print(f"Total repos:       {len(registry):,}")
    print(f"Active:            {len(active):,}")
    print(f"Skipped:           {len(skipped):,}")
    print()
    print(f"Synced today:      {len(synced_today):,}")
    print(f"Stale (synced on an earlier day): {len(stale):,}")
    print(f"Never synced:      {len(never_synced):,}")
    print(f"Last sync failed:  {len(failed):,}")
    print()


def main():
    print_registry_stats()
    disk_skills = list(load_skills())
    disk_ids = {s["id"] for s in disk_skills}
    disk_repos = {s["owner"] + "/" + s["repo"] for s in disk_skills}
    disk_skill_count = sum(1 for s in disk_skills if Path(s["path"]).name.lower() == "skill.md")

    client = get_client()

    if not client.collection_exists(COLLECTION):
        qdrant_count = 0
        qdrant_ids = set()
        qdrant_repos = set()
        qdrant_skill_count = 0
    else:
        qdrant_count = client.count(COLLECTION, exact=True).count
        qdrant_ids = set()
        qdrant_repos = set()
        qdrant_skill_count = 0
        offset = None
        while True:
            points, offset = client.scroll(
                COLLECTION,
                with_payload=["owner", "repo", "path"],
                with_vectors=False,
                limit=1000,
                offset=offset,
            )
            for p in points:
                qdrant_ids.add(p.id)
                owner = p.payload.get("owner")
                repo = p.payload.get("repo")
                if owner and repo:
                    qdrant_repos.add(f"{owner}/{repo}")
                path = p.payload.get("path")
                if path and Path(path).name.lower() == "skill.md":
                    qdrant_skill_count += 1
            if offset is None:
                break

    missing_from_qdrant = len(disk_ids - qdrant_ids)
    stale_in_qdrant = len(qdrant_ids - disk_ids)

    print("--- search-raw (disk) ---")
    print(f"Skills: {disk_skill_count:,}")
    print(f"Files:  {len(disk_skills):,}")
    print(f"Repos:  {len(disk_repos):,}")
    print(f"Dir:    {SEARCH_RAW_DIR}")
    print()
    print("--- qdrant (indexed) ---")
    print(f"Skills: {qdrant_skill_count:,}")
    print(f"Points: {qdrant_count:,}")
    print(f"Repos:  {len(qdrant_repos):,}")
    print(f"Collection: {COLLECTION!r}")
    print()
    print("--- diff ---")
    print(f"On disk but not indexed: {missing_from_qdrant:,}")
    print(f"Indexed but not on disk (stale): {stale_in_qdrant:,}")
    if missing_from_qdrant or stale_in_qdrant:
        print("-> run index_qdrant.py to sync")
    else:
        print("-> in sync")
    print()

    print("--- skills_export.csv ---")
    if CSV_FILE.exists():
        with CSV_FILE.open(newline="", encoding="utf-8") as f:
            row_count = sum(1 for _ in csv.reader(f)) - 1  # minus header
        print(f"Rows: {row_count:,}")
        print(f"File: {CSV_FILE}")
    else:
        print(f"Not found: {CSV_FILE} (run export_csv.py)")


if __name__ == "__main__":
    main()
