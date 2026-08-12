"""Read-only HTTP query service wrapping search.py's search_skills()/
browse_skills() for non-Python callers (e.g. a Next.js Route Handler) that
can't reproduce the query-time fastembed embedding step themselves -- see
docs/NEXTJS_INTEGRATION.md's "recommended" option.

Never calls upsert/set_payload/delete/create_collection -- read-only,
full stop. Connects to Qdrant the same way search.py does: server mode via
SKILLS_QDRANT_URL by default, or embedded on-disk mode via
SKILLS_QDRANT_DB_PATH if set. Embedded mode takes an exclusive file lock,
so only one process (this one) can hold it at a time.

Run locally:
    uv run uvicorn query_service:app --host 0.0.0.0 --port 8000
Or via docker compose (see docker/docker-compose.qdrant.yml).

OpenAPI schema is served automatically at /openapi.json (FastAPI).
"""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from search import (
    COLLECTION,
    SearchFilters,
    SearchResult,
    browse_skills,
    search_skills,
)
from search import _get_client as _get_client  # pyright: ignore[reportPrivateUsage]

app = FastAPI(
    title="agent-skills query service",
    description="Read-only hybrid search over the agent_skills Qdrant collection.",
    version="1.0.0",
)


class QueryRequest(BaseModel):
    query: str = Field(
        default="", description="Free-text search query; empty string browses instead."
    )
    limit: int = Field(default=12, ge=1, le=200)
    min_stars: int | None = Field(default=None, ge=0)
    sources: tuple[str, ...] = ()
    rank_filters: dict[str, int] = Field(default_factory=dict)
    languages: tuple[str, ...] = Field(
        default=(), description="Only return hits whose language is in this list."
    )
    agent_compatibility: tuple[str, ...] = Field(
        default=(), description="Only return hits whose agent_compatibility overlaps this list."
    )


class Hit(BaseModel):
    score: float | None
    rank: int
    path: str
    name: str
    owner: str
    repo: str
    repo_url: str
    skill_url: str
    description: str
    content: str
    sources: tuple[str, ...]
    stars: int | None
    ranking: str
    search_rank: dict[str, int]
    duplicate_count: int
    name_collision_count: int
    name_shared_with: tuple[str, ...]
    locations: tuple[dict[str, Any], ...]
    language: str
    agent_compatibility: tuple[str, ...]


class QueryResponse(BaseModel):
    index_ready: bool
    query: str
    hits: list[Hit]


def _to_hit(result: SearchResult) -> Hit:
    return Hit(
        score=result.score,
        rank=result.rank,
        path=result.path,
        name=result.name,
        owner=result.owner,
        repo=result.repository,
        repo_url=result.repo_url,
        skill_url=result.skill_url,
        description=result.description,
        content=result.content,
        sources=result.sources,
        stars=result.stars,
        ranking=result.ranking,
        search_rank=result.search_rank,
        duplicate_count=result.duplicate_count,
        name_collision_count=result.name_collision_count,
        name_shared_with=result.name_shared_with,
        locations=result.locations,
        language=result.language,
        agent_compatibility=result.agent_compatibility,
    )


def _index_ready() -> bool:
    """A missing/empty collection means the nightly batch job hasn't run
    yet or is broken -- not "no results" -- see docs/NEXTJS_INTEGRATION.md's
    "Empty or missing collection" section."""
    client = _get_client()
    if not client.collection_exists(COLLECTION):
        return False
    return client.count(COLLECTION, exact=False).count > 0


@app.get("/health")
def health() -> dict[str, bool]:
    return {"index_ready": _index_ready()}


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    if not _index_ready():
        return QueryResponse(index_ready=False, query=request.query, hits=[])

    filters = SearchFilters(
        min_stars=request.min_stars,
        sources=request.sources,
        rank_filters=request.rank_filters,
        languages=request.languages,
        agent_compatibility=request.agent_compatibility,
    )

    normalized_query = request.query.strip()
    results = (
        browse_skills(limit=request.limit, filters=filters)
        if not normalized_query
        else search_skills(normalized_query, limit=request.limit, filters=filters)
    )
    return QueryResponse(
        index_ready=True,
        query=request.query,
        hits=[_to_hit(r) for r in results],
    )
