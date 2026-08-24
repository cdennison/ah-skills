#!/usr/bin/env python3
"""Compare /search-raw on disk vs what's actually indexed in Qdrant, plus a
breakdown of what's actually in the index -- ranking coverage, language,
agent-target classification, source channel, and duplication.

Run any time to sanity-check that the last index_qdrant.py run is caught up,
or just to get a snapshot of what the index currently looks like.
"""

import csv
import datetime
from collections import Counter
from pathlib import Path

csv.field_size_limit(10_000_000)  # export_csv.py's writer has no such cap on
# write, so a single large "content" (or duplicate-heavy "locations") field
# can exceed the csv module's read-side default (128KB) -- raise it well past
# the largest skill file we've actually seen (~567KB) so counting rows here
# doesn't crash on legitimately large-but-not-corrupted content.

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

    language_counts: Counter = Counter()
    agent_counts: Counter = Counter()
    source_counts: Counter = Counter()
    ranked_skill_count = 0
    duplicate_skill_count = 0

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
                with_payload=[
                    "owner", "repo", "path", "language", "agent_compatibility",
                    "ranking", "sources", "duplicate_count",
                ],
                with_vectors=False,
                limit=1000,
                offset=offset,
            )
            for p in points:
                qdrant_ids.add(p.id)
                payload = p.payload or {}
                owner = payload.get("owner")
                repo = payload.get("repo")
                if owner and repo:
                    qdrant_repos.add(f"{owner}/{repo}")
                path = payload.get("path")
                is_skill = bool(path) and Path(path).name.lower() == "skill.md"
                if is_skill:
                    qdrant_skill_count += 1
                    # Breakdowns below are skill-level (one vote per SKILL.md),
                    # not per-point -- an "extra README" point would otherwise
                    # double-count its repo's language/agent/source/ranking.
                    language_counts[payload.get("language") or "en"] += 1
                    agents = payload.get("agent_compatibility") or []
                    if agents:
                        for a in agents:
                            agent_counts[a] += 1
                    else:
                        agent_counts["(unclassified)"] += 1
                    if payload.get("ranking"):
                        ranked_skill_count += 1
                    for s in payload.get("sources") or []:
                        source_counts[s] += 1
                    if (payload.get("duplicate_count") or 1) > 1:
                        duplicate_skill_count += 1
            if offset is None:
                break

    missing_from_qdrant = len(disk_ids - qdrant_ids)
    stale_in_qdrant = len(qdrant_ids - disk_ids)

    def pct(n):
        return f"{n:,} ({100 * n / qdrant_skill_count:.1f}%)" if qdrant_skill_count else f"{n:,}"

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
    print("--- ranking coverage ---")
    print(f"Skills with any ranking/popularity data: {pct(ranked_skill_count)}")
    print(f"Skills with no ranking data (seed/manual/marketplace only): {pct(qdrant_skill_count - ranked_skill_count)}")
    print()
    print("--- by source channel (a skill can count toward more than one) ---")
    for source, count in source_counts.most_common():
        print(f"{source:<20} {pct(count)}")
    print()
    print("--- by content language ---")
    for language, count in language_counts.most_common():
        print(f"{language:<20} {pct(count)}")
    print()
    print("--- by agent-target classification (a skill can count toward more than one) ---")
    for agent, count in agent_counts.most_common():
        print(f"{agent:<20} {pct(count)}")
    print()
    print("--- duplication ---")
    print(f"Same content found under >1 repo/path: {pct(duplicate_skill_count)}")
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
