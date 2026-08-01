#!/usr/bin/env python3
"""Index SKILL.md files from /search-raw into a local Qdrant collection.

Uses Qdrant's built-in FastEmbed integration (models.Document) so embedding
happens automatically on upload/query -- no separate embedding step needed.
"""

import re
from pathlib import Path

from qdrant_client import QdrantClient, models

SEARCH_RAW_DIR = Path(__file__).parent / "search-raw"
DB_PATH = Path(__file__).parent / "qdrant_db"
COLLECTION = "agent_skills"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL_NAME = "Qdrant/bm25"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"


def parse_frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def load_skills():
    for path in sorted(SEARCH_RAW_DIR.rglob("*.md")):
        rel = path.relative_to(SEARCH_RAW_DIR)
        text = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        yield {
            "path": str(rel),
            "repo": rel.parts[0],
            "name": meta.get("name", path.parent.name),
            "description": meta.get("description", ""),
            "content": text,
        }


def main():
    client = QdrantClient(path=str(DB_PATH))

    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)

    client.create_collection(
        COLLECTION,
        vectors_config={
            DENSE_VECTOR_NAME: models.VectorParams(
                size=client.get_embedding_size(MODEL_NAME),
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: models.SparseVectorParams(
                modifier=models.Modifier.IDF,
            ),
        },
    )

    skills = list(load_skills())
    vectors = [
        {
            DENSE_VECTOR_NAME: models.Document(
                text=f"{s['name']}: {s['description']}\n\n{s['content']}", model=MODEL_NAME
            ),
            SPARSE_VECTOR_NAME: models.Document(
                text=f"{s['name']}: {s['description']}\n\n{s['content']}", model=SPARSE_MODEL_NAME
            ),
        }
        for s in skills
    ]

    client.upload_collection(
        collection_name=COLLECTION,
        vectors=vectors,
        payload=skills,
        ids=list(range(len(skills))),
    )

    print(f"Indexed {len(skills)} skill files into {DB_PATH} (collection={COLLECTION!r})")


if __name__ == "__main__":
    main()
