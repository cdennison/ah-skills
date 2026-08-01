#!/usr/bin/env python3
"""CLI to semantically search indexed SKILL.md files.

Usage:
    .venv/bin/python query.py "excel spreadsheets" [-n 5]
"""

import argparse

from qdrant_client import QdrantClient, models

from index_qdrant import (
    COLLECTION,
    DB_PATH,
    DENSE_VECTOR_NAME,
    MODEL_NAME,
    SPARSE_MODEL_NAME,
    SPARSE_VECTOR_NAME,
)


def main():
    parser = argparse.ArgumentParser(description="Search indexed agent skills")
    parser.add_argument("query", help="natural language search query")
    parser.add_argument("-n", "--limit", type=int, default=5, help="number of results (default 5)")
    args = parser.parse_args()

    client = QdrantClient(path=str(DB_PATH))
    results = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(
                query=models.Document(text=args.query, model=MODEL_NAME),
                using=DENSE_VECTOR_NAME,
                limit=20,
            ),
            models.Prefetch(
                query=models.Document(text=args.query, model=SPARSE_MODEL_NAME),
                using=SPARSE_VECTOR_NAME,
                limit=20,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=args.limit,
    )

    for hit in results.points:
        print(f"{hit.score:.3f}  {hit.payload['path']}")
        if hit.payload.get("description"):
            print(f"       {hit.payload['description']}")


if __name__ == "__main__":
    main()
