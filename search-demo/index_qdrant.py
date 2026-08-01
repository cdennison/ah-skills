#!/usr/bin/env python3
"""Index SKILL.md files from /search-raw into a local Qdrant collection.

Uses Qdrant's built-in FastEmbed integration (models.Document) so embedding
happens automatically on upload/query -- no separate embedding step needed.

Incremental: each point's id is a hash of its relative path, and its payload
carries a content hash. Re-running only (re-)embeds files that are new or
whose content changed, and removes points for files that disappeared. A
from-scratch run (empty/missing collection) costs the same as before.
"""

import hashlib
import re
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

SEARCH_RAW_DIR = Path(__file__).parent / "search-raw"
DB_PATH = Path(__file__).parent / "qdrant_db"
COLLECTION = "agent_skills"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL_NAME = "Qdrant/bm25"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Qdrant point ids must be an unsigned int or a UUID -- an arbitrary hex
# digest is rejected, so derive a stable UUID5 from the relative path
# instead (same path always maps to the same id across runs).
POINT_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def point_id(rel_path: str) -> str:
    return str(uuid.uuid5(POINT_ID_NAMESPACE, rel_path))


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def parse_frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    fields = {}
    lines = match.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line or line.startswith((" ", "\t")):
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            # YAML block scalar: value is the indented lines that follow.
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i])
                i += 1
            # Fold ">" style like YAML does; keep "|" style line breaks as-is.
            dedented = "\n".join(l.lstrip() for l in block).strip()
            fields[key] = dedented if value.startswith("|") else " ".join(dedented.splitlines())
            continue
        fields[key] = value.strip('"').strip("'")
        i += 1
    return fields


def load_skills():
    for path in sorted(SEARCH_RAW_DIR.rglob("*.md")):
        rel = path.relative_to(SEARCH_RAW_DIR)
        owner, repo = rel.parts[0], rel.parts[1]
        subpath = "/".join(rel.parts[2:])
        text = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        yield {
            "id": point_id(str(rel)),
            "path": str(rel),
            "owner": owner,
            "repo": repo,
            "repo_url": f"https://github.com/{owner}/{repo}",
            # blob/HEAD resolves to whatever the default branch currently is,
            # so this stays valid even if a repo renames main/master later.
            "skill_url": f"https://github.com/{owner}/{repo}/blob/HEAD/{subpath}",
            "name": meta.get("name", path.parent.name),
            "description": meta.get("description", ""),
            "content": text,
            "content_hash": content_hash(text),
        }


def existing_hashes(client: QdrantClient) -> dict:
    """Map of point id -> content_hash currently stored in the collection."""
    hashes = {}
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION,
            with_payload=["content_hash"],
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        for p in points:
            hashes[p.id] = p.payload.get("content_hash")
        if offset is None:
            break
    return hashes


def main():
    client = QdrantClient(path=str(DB_PATH))

    if not client.collection_exists(COLLECTION):
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
    current_ids = {s["id"] for s in skills}
    old_hashes = existing_hashes(client)

    changed = [s for s in skills if old_hashes.get(s["id"]) != s["content_hash"]]
    stale_ids = [pid for pid in old_hashes if pid not in current_ids]

    if stale_ids:
        client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale_ids))

    if changed:
        vectors = [
            {
                DENSE_VECTOR_NAME: models.Document(
                    text=f"{s['name']}: {s['description']}\n\n{s['content']}", model=MODEL_NAME
                ),
                SPARSE_VECTOR_NAME: models.Document(
                    text=f"{s['name']}: {s['description']}\n\n{s['content']}", model=SPARSE_MODEL_NAME
                ),
            }
            for s in changed
        ]
        client.upload_collection(
            collection_name=COLLECTION,
            vectors=vectors,
            payload=changed,
            ids=[s["id"] for s in changed],
        )

    print(
        f"Indexed {len(skills)} skill files into {DB_PATH} (collection={COLLECTION!r}): "
        f"{len(changed)} new/changed, {len(stale_ids)} removed, "
        f"{len(skills) - len(changed)} unchanged"
    )


if __name__ == "__main__":
    main()
