"""MCP-server search, mirroring search.py's shape (SkillPayload/SearchResult/
search_skills/browse_skills) but over the `mcp_servers` Qdrant collection
instead of `agent_skills`.

Self-contained rather than importing from ../shared/ (which index_qdrant.py
in both ../ and ../mcp-search/ use): this app/ directory is its own
separately-dependency-managed, separately-Dockerized project (own
pyproject.toml/uv.lock; docker-compose.qdrant.yml's build context is
`../app` alone, which doesn't include ../shared/). Duplicating the small
client-construction pattern here is a deliberate, documented exception to
"don't duplicate" for that real deployment-boundary reason -- see
../shared/qdrant.py's own docstring for the other half of this note.

MCP_QDRANT_URL/MCP_QDRANT_DB_PATH deliberately mirror search.py's
SKILLS_QDRANT_URL/SKILLS_QDRANT_DB_PATH naming but in the MCP pipeline's own
namespace -- both point at the same Qdrant server by default (one server,
two collections), but can be overridden independently.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

ROOT_DIR: Final = Path(__file__).resolve().parent.parent
QDRANT_URL: Final = os.environ.get("MCP_QDRANT_URL", os.environ.get("SKILLS_QDRANT_URL", "http://localhost:6333"))
_db_path_override = os.environ.get("MCP_QDRANT_DB_PATH")
DB_PATH: Final = Path(_db_path_override) if _db_path_override else None
COLLECTION: Final = "mcp_servers"
MODEL_NAME: Final = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL_NAME: Final = "Qdrant/bm25"
DENSE_VECTOR_NAME: Final = "dense"
SPARSE_VECTOR_NAME: Final = "sparse"

CLIENT_TIMEOUT_SECONDS: Final = 300  # matches search.py's own -- qdrant-client's
# default (5s) is too short for a scroll/query against a large collection
# under load.

_client: QdrantClient | None = None


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = (
            QdrantClient(path=str(DB_PATH)) if DB_PATH else QdrantClient(url=QDRANT_URL, timeout=CLIENT_TIMEOUT_SECONDS)
        )
    return _client


class McpPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    mcp_id: str
    name: str
    description: str = ""
    readme: str = ""
    repo_url: str | None = None
    status: str = "active"
    mcp_category: str | None = None
    mcp_category_source: str | None = None
    sources: tuple[str, ...] = ()
    source_count: int = 0
    registry_type: str | None = None
    package_identifier: str | None = None
    package_url: str | None = None
    deployment: str | None = None
    has_installable_package: bool = False
    has_remote: bool = False
    attributes: tuple[str, ...] = ()
    license: str | None = None
    added: str | None = None


@dataclass(frozen=True, slots=True)
class McpSearchResult:
    rank: int
    score: float | None
    mcp_id: str
    name: str
    description: str
    readme: str
    repo_url: str | None
    status: str
    mcp_category: str | None
    mcp_category_source: str | None
    sources: tuple[str, ...]
    registry_type: str | None
    package_identifier: str | None
    package_url: str | None
    deployment: str | None
    has_installable_package: bool
    has_remote: bool
    attributes: tuple[str, ...]
    license: str | None
    added: str | None


@dataclass(frozen=True, slots=True)
class McpSearchFilters:
    """Widget/query-driven filters, pushed down to Qdrant as native payload
    filters -- same approach as search.py's SearchFilters."""

    mcp_category: tuple[str, ...] = ()
    deployment: tuple[str, ...] = ()
    registry_type: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def is_active(self) -> bool:
        return bool(self.mcp_category or self.deployment or self.registry_type or self.sources)


def filters_to_qdrant_filter(filters: McpSearchFilters | None) -> models.Filter | None:
    if filters is None:
        return None
    conditions: list[models.Condition] = []
    if filters.mcp_category:
        conditions.append(models.FieldCondition(key="mcp_category", match=models.MatchAny(any=list(filters.mcp_category))))
    if filters.deployment:
        conditions.append(models.FieldCondition(key="deployment", match=models.MatchAny(any=list(filters.deployment))))
    if filters.registry_type:
        conditions.append(models.FieldCondition(key="registry_type", match=models.MatchAny(any=list(filters.registry_type))))
    if filters.sources:
        conditions.append(models.FieldCondition(key="sources", match=models.MatchAny(any=list(filters.sources))))
    return models.Filter(must=conditions) if conditions else None


def build_search_result(*, rank: int, payload: McpPayload, score: float | None) -> McpSearchResult:
    return McpSearchResult(
        rank=rank,
        score=score,
        mcp_id=payload.mcp_id,
        name=payload.name,
        description=payload.description,
        readme=payload.readme,
        repo_url=payload.repo_url,
        status=payload.status,
        mcp_category=payload.mcp_category,
        mcp_category_source=payload.mcp_category_source,
        sources=payload.sources,
        registry_type=payload.registry_type,
        package_identifier=payload.package_identifier,
        package_url=payload.package_url,
        deployment=payload.deployment,
        has_installable_package=payload.has_installable_package,
        has_remote=payload.has_remote,
        attributes=payload.attributes,
        license=payload.license,
        added=payload.added,
    )


def search_mcp_servers(
    query: str, *, limit: int = 12, filters: McpSearchFilters | None = None
) -> list[McpSearchResult]:
    normalized_query = query.strip()
    if not normalized_query:
        return []

    qdrant_filter = filters_to_qdrant_filter(filters)
    client = _get_client()
    response = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(
                query=models.Document(text=normalized_query, model=MODEL_NAME),
                using=DENSE_VECTOR_NAME,
                limit=max(limit, 20),
                filter=qdrant_filter,
            ),
            models.Prefetch(
                query=models.Document(text=normalized_query, model=SPARSE_MODEL_NAME),
                using=SPARSE_VECTOR_NAME,
                limit=max(limit, 20),
                filter=qdrant_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=qdrant_filter,
        limit=limit,
    )
    return [
        build_search_result(rank=rank, payload=McpPayload.model_validate(hit.payload), score=hit.score)
        for rank, hit in enumerate(response.points, start=1)
    ]


def browse_mcp_servers(*, limit: int = 12, filters: McpSearchFilters) -> list[McpSearchResult]:
    """Filter-only browsing for a blank query -- ordered by name (no
    stars-equivalent popularity signal indexed yet for MCP servers, unlike
    search.py's browse_skills(), which sorts by stars)."""
    qdrant_filter = filters_to_qdrant_filter(filters)
    client = _get_client()
    candidates: list[McpPayload] = []
    offset = None
    target = max(limit, 20) * 5
    while len(candidates) < target:
        points, offset = client.scroll(
            COLLECTION,
            scroll_filter=qdrant_filter,
            with_payload=True,
            with_vectors=False,
            limit=min(500, target - len(candidates)),
            offset=offset,
        )
        if not points:
            break
        candidates.extend(McpPayload.model_validate(p.payload) for p in points)
        if offset is None:
            break

    candidates.sort(key=lambda p: p.name.lower())

    return [
        build_search_result(rank=rank, payload=payload, score=None)
        for rank, payload in enumerate(candidates[:limit], start=1)
    ]
