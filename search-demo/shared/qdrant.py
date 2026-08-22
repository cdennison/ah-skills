"""Shared Qdrant plumbing for this repo's two independent indexing
pipelines (skills: ../index_qdrant.py, MCP: ../mcp-search/index_qdrant.py).

Deliberately just the mechanical bits -- client construction, embedding
model construction with bounded onnxruntime threads/memory, and size-capped
batch upserts -- never collection names, payload schemas, or point-id
semantics, which differ by design between the two pipelines and stay local
to each script (see ../mcp-search/PROPOSED_PIPELINE.md's "why separate
storage isn't just tidiness" section for why these two pipelines
deliberately don't share state, only this kind of mechanical code).

NOT used by ../app/ (the FastAPI/Streamlit query service): that directory
is its own separately-dependency-managed, separately-Dockerized project
(own pyproject.toml/uv.lock, and docker-compose.qdrant.yml's build context
is `../app` alone, which does not include this shared/ directory) -- so its
client-construction code stays self-contained rather than importing here,
a deliberate, documented exception to "don't duplicate" for a real
deployment-boundary reason, not an oversight.
"""

import os

from qdrant_client import QdrantClient, models

CLIENT_TIMEOUT_SECONDS = 300  # qdrant-client's own default (5s) is too short
# for bulk delete/scroll/upload calls against a large collection -- e.g. a
# single large delete/upsert has been observed to take 20s+ server-side
# (and succeed), well past the 5s default, causing the client to raise
# ResponseHandlingException("timed out") on a call that actually completed
# fine on the Qdrant side.

MAX_UPSERT_BYTES = 24 * 1024 * 1024  # stay under Qdrant's 32MB default HTTP
# request-size limit. Point size varies wildly with payload text length and
# sparse-vector length (which scales with unique-token count, not
# document length) -- a fixed points-per-call count alone isn't a safe
# bound: a 10,000-point chunk once produced a 273MB request, and even 500
# points once produced 45MB. Sub-split by actual serialized size instead.


def get_client(url_env: str, db_path_env: str, default_url: str = "http://localhost:6333") -> QdrantClient:
    """`url_env`/`db_path_env` are the CALLER's own env var names (e.g.
    "SKILLS_QDRANT_URL"/"SKILLS_QDRANT_DB_PATH" or "MCP_QDRANT_URL"/
    "MCP_QDRANT_DB_PATH") -- passed in rather than hardcoded here so the
    two pipelines' env-var namespaces can never collide, even by accident.
    `db_path_env`, if set, selects an embedded on-disk store instead of the
    Docker server at `default_url`."""
    db_path_override = os.environ.get(db_path_env)
    if db_path_override:
        return QdrantClient(path=db_path_override)
    return QdrantClient(url=os.environ.get(url_env, default_url), timeout=CLIENT_TIMEOUT_SECONDS)


_embedder_cache: dict[str, object] = {}


def get_embedder(model_name: str, sparse: bool, threads: int):
    """Construct (once, cached) a dense (TextEmbedding) or sparse
    (SparseTextEmbedding) fastembed model with an explicit thread count and
    the CPU memory arena disabled. Going through fastembed directly --
    rather than qdrant_client's automatic `models.Document` inference --
    is what makes these onnxruntime knobs reachable at all.

    Left at onnxruntime's default (one thread pool *per CPU core*, per
    model, each with its own ever-growing CPU-arena allocator that never
    shrinks back), this is what drove anon-RSS to ~2.3GB indexing just
    1000 short skill documents on a 3.7GB box. `enable_cpu_mem_arena=False`
    trades a bit of per-call allocation overhead for a bounded footprint
    instead of an ever-growing one."""
    from fastembed import SparseTextEmbedding, TextEmbedding

    cache_key = f"{model_name}:{threads}"
    if cache_key not in _embedder_cache:
        cls = SparseTextEmbedding if sparse else TextEmbedding
        _embedder_cache[cache_key] = cls(model_name=model_name, threads=threads, enable_cpu_mem_arena=False)
    return _embedder_cache[cache_key]


def estimate_point_bytes(point: models.PointStruct) -> int:
    """Exact serialized size of this point, matching what the HTTP request
    actually transmits -- deliberately not a cheap text-length
    approximation (an earlier version undercounted the BM25 sparse
    vector's true cost badly; see upsert_size_capped)."""
    return len(point.model_dump_json().encode("utf-8"))


def upsert_size_capped(client: QdrantClient, collection: str, points: list[models.PointStruct]) -> None:
    """Sub-split `points` into <=MAX_UPSERT_BYTES chunks by real serialized
    size, regardless of point count -- see MAX_UPSERT_BYTES for why a fixed
    points-per-call batch size alone isn't a safe bound."""
    sub_batch: list[models.PointStruct] = []
    sub_batch_bytes = 0
    for point in points:
        point_bytes = estimate_point_bytes(point)
        if sub_batch and sub_batch_bytes + point_bytes > MAX_UPSERT_BYTES:
            client.upsert(collection_name=collection, points=sub_batch)
            sub_batch = []
            sub_batch_bytes = 0
        sub_batch.append(point)
        sub_batch_bytes += point_bytes
    if sub_batch:
        client.upsert(collection_name=collection, points=sub_batch)
