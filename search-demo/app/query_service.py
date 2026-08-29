"""Read-only HTTP query service wrapping search.py's search_skills()/
browse_skills() (agent_skills collection) AND mcp_search.py's
search_mcp_servers()/browse_mcp_servers() (mcp_servers collection) --
callers pick which with the `asset_type` field ("skill" or "mcp") on every
request. For non-Python callers (e.g. a Next.js Route Handler) that can't
reproduce the query-time fastembed embedding step themselves -- see
docs/NEXTJS_INTEGRATION.md's "recommended" option.

Read-only for search and browse -- never upsert/delete/create_collection.
The one exception is POST /scan/skill (scan_index.scan_and_record), which
writes a single top-level `llm_scan` payload key back onto one agent_skills
point via set_payload; it still never touches a vector or creates a
collection. Connects to Qdrant the same way search.py/mcp_search.py do:
server mode via SKILLS_QDRANT_URL/MCP_QDRANT_URL by default (same server,
two collections), or embedded on-disk mode via
SKILLS_QDRANT_DB_PATH/MCP_QDRANT_DB_PATH if set.

Run locally:
    uv run uvicorn query_service:app --host 0.0.0.0 --port 8000
Or via docker compose (see docker/docker-compose.qdrant.yml).

OpenAPI schema is served automatically at /openapi.json (FastAPI).
"""

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import mcp_search
import search
from mcp_search import McpSearchFilters, McpSearchResult, browse_mcp_servers, search_mcp_servers
from scan_index import (
    ScanSkillRequest,
    ScanSkillResponse,
    SkillNotFound,
    scan_and_record,
)
from scan_service import (
    ScanConfigError,
    ScanRequest,
    ScanResponse,
    ScanUpstreamError,
    scan_skill_text,
)
from search import SearchFilters, SearchResult, browse_skills, search_skills

app = FastAPI(
    title="agent-skills / mcp-servers query service",
    description=(
        "Hybrid search over the agent_skills and mcp_servers Qdrant collections "
        "(read-only), a non-deterministic LLM threat scan for skill text "
        "(POST /scan), and a scan-and-record path that writes the verdict onto "
        "the skill's Qdrant point (POST /scan/skill)."
    ),
    version="1.3.0",
)

AssetType = Literal["skill", "mcp"]


class QueryRequest(BaseModel):
    query: str = Field(
        default="", description="Free-text search query; empty string browses instead."
    )
    asset_type: AssetType = Field(
        default="skill", description="Which collection to search: 'skill' (agent_skills) or 'mcp' (mcp_servers)."
    )
    limit: int = Field(default=12, ge=1, le=200)

    # skill-only filters (ignored when asset_type="mcp")
    min_stars: int | None = Field(default=None, ge=0)
    sources: tuple[str, ...] = ()
    rank_filters: dict[str, int] = Field(default_factory=dict)
    languages: tuple[str, ...] = Field(
        default=(), description="skill only: only return hits whose language is in this list."
    )
    agent_compatibility: tuple[str, ...] = Field(
        default=(), description="skill only: only return hits whose agent_compatibility overlaps this list."
    )

    # mcp-only filters (ignored when asset_type="skill")
    mcp_category: tuple[str, ...] = Field(
        default=(), description="mcp only: filter to server/client/framework/tooling."
    )
    deployment: tuple[str, ...] = Field(
        default=(), description="mcp only: filter to local/remote/hybrid."
    )
    registry_type: tuple[str, ...] = Field(
        default=(), description="mcp only: filter to npm/pypi/oci/etc."
    )


class SkillHit(BaseModel):
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
    # Latest LLM threat-scan verdict (scan_index.LlmScan shape) once the skill
    # has been through POST /scan/skill; null otherwise. The deterministic
    # Vettd scan is separate -- it rides inside each `locations[]` entry as
    # `vettd_scan_findings` / `vettd_scan_publications`.
    llm_scan: dict[str, Any] | None


class McpHit(BaseModel):
    score: float | None
    rank: int
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


class QueryResponse(BaseModel):
    index_ready: bool
    query: str
    asset_type: AssetType
    hits: list[SkillHit] | list[McpHit]


def _to_skill_hit(result: SearchResult) -> SkillHit:
    return SkillHit(
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
        llm_scan=result.llm_scan,
    )


def _to_mcp_hit(result: McpSearchResult) -> McpHit:
    return McpHit(
        score=result.score,
        rank=result.rank,
        mcp_id=result.mcp_id,
        name=result.name,
        description=result.description,
        readme=result.readme,
        repo_url=result.repo_url,
        status=result.status,
        mcp_category=result.mcp_category,
        mcp_category_source=result.mcp_category_source,
        sources=result.sources,
        registry_type=result.registry_type,
        package_identifier=result.package_identifier,
        package_url=result.package_url,
        deployment=result.deployment,
        has_installable_package=result.has_installable_package,
        has_remote=result.has_remote,
        attributes=result.attributes,
        license=result.license,
        added=result.added,
    )


def _index_ready(asset_type: AssetType) -> bool:
    """A missing/empty collection means the relevant batch job hasn't run
    yet or is broken -- not "no results" -- see docs/NEXTJS_INTEGRATION.md's
    "Empty or missing collection" section."""
    if asset_type == "mcp":
        client = mcp_search._get_client()
        collection = mcp_search.COLLECTION
    else:
        client = search._get_client()
        collection = search.COLLECTION
    if not client.collection_exists(collection):
        return False
    return client.count(collection, exact=False).count > 0


@app.get("/health")
def health(asset_type: AssetType = "skill") -> dict[str, Any]:
    return {"asset_type": asset_type, "index_ready": _index_ready(asset_type)}


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    if not _index_ready(request.asset_type):
        return QueryResponse(index_ready=False, query=request.query, asset_type=request.asset_type, hits=[])

    normalized_query = request.query.strip()

    if request.asset_type == "mcp":
        mcp_filters = McpSearchFilters(
            mcp_category=request.mcp_category,
            deployment=request.deployment,
            registry_type=request.registry_type,
            sources=request.sources,
        )
        mcp_results = (
            browse_mcp_servers(limit=request.limit, filters=mcp_filters)
            if not normalized_query
            else search_mcp_servers(normalized_query, limit=request.limit, filters=mcp_filters)
        )
        return QueryResponse(
            index_ready=True, query=request.query, asset_type="mcp", hits=[_to_mcp_hit(r) for r in mcp_results]
        )

    skill_filters = SearchFilters(
        min_stars=request.min_stars,
        sources=request.sources,
        rank_filters=request.rank_filters,
        languages=request.languages,
        agent_compatibility=request.agent_compatibility,
    )
    skill_results = (
        browse_skills(limit=request.limit, filters=skill_filters)
        if not normalized_query
        else search_skills(normalized_query, limit=request.limit, filters=skill_filters)
    )
    return QueryResponse(
        index_ready=True, query=request.query, asset_type="skill", hits=[_to_skill_hit(r) for r in skill_results]
    )


@app.post(
    "/scan",
    response_model=ScanResponse,
    responses={
        502: {"description": "Upstream LLM call failed or returned unparseable output"},
        503: {"description": "Scan LLM not configured (no API key)"},
    },
)
def scan(request: ScanRequest) -> ScanResponse:
    """Non-deterministic threat scan: send the skill text to an LLM (litellm /
    OpenRouter) with the Cisco threat-analysis prompt and return its structured
    findings / overall_assessment / primary_threats verdict. See scan_service."""
    try:
        return scan_skill_text(request.skill_text, request.skill_name, request.model)
    except ScanConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScanUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/scan/skill",
    response_model=ScanSkillResponse,
    responses={
        404: {"description": "No agent_skills point matched point_id / content_hash"},
        502: {"description": "Upstream LLM call failed or returned unparseable output"},
        503: {"description": "Scan LLM not configured (no API key)"},
    },
)
def scan_skill(request: ScanSkillRequest) -> ScanSkillResponse:
    """Scan an already-indexed skill and record the verdict on its Qdrant point.

    Identify the skill by `point_id` (or `content_hash`); the service reads its
    SKILL.md text, runs the same non-deterministic scan as POST /scan, writes a
    top-level `llm_scan` payload field, and returns it. A recent scan for
    unchanged content + model + prompt is reused (`skipped: true`) unless
    `force: true`. See scan_index."""
    try:
        return scan_and_record(request)
    except SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScanConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScanUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
