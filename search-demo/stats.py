#!/usr/bin/env python3
"""Compare /search-raw on disk vs what's actually indexed in Qdrant.

Run any time to sanity-check that the last index_qdrant.py run is caught up.
"""

from pathlib import Path

from qdrant_client import QdrantClient

from index_qdrant import COLLECTION, DB_PATH, SEARCH_RAW_DIR, load_skills

def main():
    disk_skills = list(load_skills())
    disk_ids = {s["id"] for s in disk_skills}
    disk_repos = {s["owner"] + "/" + s["repo"] for s in disk_skills}

    client = QdrantClient(path=str(DB_PATH))

    if not client.collection_exists(COLLECTION):
        qdrant_count = 0
        qdrant_ids = set()
        qdrant_repos = set()
    else:
        qdrant_count = client.count(COLLECTION, exact=True).count
        qdrant_ids = set()
        qdrant_repos = set()
        offset = None
        while True:
            points, offset = client.scroll(
                COLLECTION,
                with_payload=["owner", "repo"],
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
            if offset is None:
                break

    missing_from_qdrant = len(disk_ids - qdrant_ids)
    stale_in_qdrant = len(qdrant_ids - disk_ids)

    print("--- search-raw (disk) ---")
    print(f"Files:  {len(disk_skills):,}")
    print(f"Repos:  {len(disk_repos):,}")
    print(f"Dir:    {SEARCH_RAW_DIR}")
    print()
    print("--- qdrant (indexed) ---")
    print(f"Points: {qdrant_count:,}")
    print(f"Repos:  {len(qdrant_repos):,}")
    print(f"DB:     {DB_PATH} (collection={COLLECTION!r})")
    print()
    print("--- diff ---")
    print(f"On disk but not indexed: {missing_from_qdrant:,}")
    print(f"Indexed but not on disk (stale): {stale_in_qdrant:,}")
    if missing_from_qdrant or stale_in_qdrant:
        print("-> run index_qdrant.py to sync")
    else:
        print("-> in sync")


if __name__ == "__main__":
    main()
