#!/usr/bin/env python3
"""Build a small, fast local Qdrant snapshot from the main `qdrant_db/`
collection -- a copy, not a re-index: points (including their already-
computed dense/sparse vectors) are scrolled from the source and upserted
into a new collection at a separate path, so no embedding model runs and no
re-derivation from search-raw/ happens.

Selection: keep only points whose primary skill has a known agent target
(agent_target.classify_from_metadata() != "unknown" -- a real coding-agent
attribution via plugin-manifest/path-token/name-mention signals, see
agent_target.py), then take the top `--limit` of those by stars (repos with
unknown stars sort last).

Local embedded Qdrant isn't meant for collections much past ~20k points
(see the UserWarning it prints itself, and docs/QUERY_INTERFACE.md); this
snapshot exists purely so `app/search.py`/`app/streamlit_app.py` can point
at something that opens fast for manual testing.

Usage:
    python3 make_small_index.py [--limit 10000] [--output qdrant_db_small]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from qdrant_client import QdrantClient, models

from agent_target import classify_from_metadata
from index_qdrant import (
    COLLECTION,
    DENSE_VECTOR_NAME,
    MODEL_NAME,
    SPARSE_VECTOR_NAME,
    get_client,
)

DEFAULT_LIMIT = 10_000


def iter_source_points(client: QdrantClient, scan_limit: int | None = None):
    offset = None
    scanned = 0
    while True:
        points, offset = client.scroll(
            COLLECTION,
            with_payload=True,
            with_vectors=True,
            limit=1000,
            offset=offset,
        )
        for point in points:
            if scan_limit is not None and scanned >= scan_limit:
                return
            yield point
            scanned += 1
        if offset is None:
            break


def has_agent_target(payload: dict) -> bool:
    result = classify_from_metadata(
        path=payload.get("path", ""),
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        owner=payload.get("owner", ""),
        repo=payload.get("repo", ""),
    )
    targets = result.get("agent_targets", [])
    return bool(targets) and targets != ["unknown"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max points to keep (default {DEFAULT_LIMIT}).")
    parser.add_argument(
        "--scan-limit", type=int, default=None,
        help="Stop scanning the source collection after this many points (default: scan everything). "
        "Trades a truly-top-by-stars selection for speed -- results come from an arbitrary prefix of "
        "the source collection, not a global top-N.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).parent / "qdrant_db_small",
        help="Destination local Qdrant path (default qdrant_db_small/).",
    )
    args = parser.parse_args()

    source = get_client()
    if not source.collection_exists(COLLECTION):
        raise SystemExit(f"Source collection {COLLECTION!r} not found")

    print("Scanning source collection for points with a known agent target...")
    kept: list = []
    scanned = 0
    for point in iter_source_points(source, scan_limit=args.scan_limit):
        scanned += 1
        payload = point.payload or {}
        if has_agent_target(payload):
            kept.append(point)
        if scanned % 20_000 == 0:
            print(f"  scanned {scanned}, kept {len(kept)} so far")
    source.close()

    print(f"Scanned {scanned} points; {len(kept)} have a known agent target.")

    kept.sort(key=lambda p: ((p.payload or {}).get("stars") is None, -((p.payload or {}).get("stars") or 0)))
    kept = kept[: args.limit]
    print(f"Keeping top {len(kept)} by stars (limit={args.limit}).")

    if args.output.exists():
        raise SystemExit(f"{args.output} already exists -- remove it first if you want to rebuild the snapshot.")

    dest = QdrantClient(path=str(args.output))
    dest.create_collection(
        COLLECTION,
        vectors_config={
            DENSE_VECTOR_NAME: models.VectorParams(
                size=dest.get_embedding_size(MODEL_NAME),
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: models.SparseVectorParams(
                modifier=models.Modifier.IDF,
            ),
        },
    )

    batch_size = 500
    for i in range(0, len(kept), batch_size):
        batch = kept[i : i + batch_size]
        dest.upsert(
            COLLECTION,
            points=[
                models.PointStruct(id=p.id, vector=p.vector, payload=p.payload)
                for p in batch
            ],
        )
        print(f"  upserted {min(i + batch_size, len(kept))}/{len(kept)}")
    dest.close()

    print(f"Snapshot ready at {args.output} ({len(kept)} points).")
    print(f"Point the app at it with: SKILLS_QDRANT_DB_PATH={args.output} uv run --project app streamlit run app/streamlit_app.py")


if __name__ == "__main__":
    main()
