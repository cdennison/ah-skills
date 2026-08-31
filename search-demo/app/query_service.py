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
from pydantic import BaseModel, ConfigDict, Field

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
    version="1.4.0",
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

    # min_stars applies to BOTH asset types (skills always; mcp since the
    # mcp_servers payload carries `stars` -- pushed down as a native Qdrant
    # range filter in each collection).
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


# One fully-populated example per hit type, for human review of the response
# shape (rendered into docs/QUERY_SERVICE_API.md by scripts/render_openapi_md.sh).
# Field values are real observations assembled from the v0.2-v0.4a e2e runs
# (vettd-e2e/E2E_TEST_PLAN_0{2,3,4}.md): the skill is `affaan-m/ECC/skills/e2e-testing`
# (the one skill that carries all three scan verdicts); the MCP server is
# `github:upstash/context7`. `content` / `readme` and the full advisory-id list
# are truncated for readability; everything else is a real value.
_SKILL_HIT_EXAMPLE: dict[str, Any] = {
    "score": 0.87,
    "rank": 1,
    "path": "affaan-m/ECC/skills/e2e-testing/SKILL.md",
    "name": "e2e-testing",
    "owner": "affaan-m",
    "repo": "ECC",
    "repo_url": "https://github.com/affaan-m/ECC",
    "skill_url": "https://github.com/affaan-m/ECC/blob/HEAD/skills/e2e-testing/SKILL.md",
    "description": (
        "Drives end-to-end browser tests with Playwright: installs the runner, "
        "generates specs from a user story, runs them headless with retries, and "
        "triages failures. Use when a task needs real-browser verification of a web app."
    ),
    "content": "<full SKILL.md text plus any scripts -- truncated in this example>",
    "sources": ["marketplace", "search"],
    "stars": 240095,
    "ranking": "skills_sh_rank=142 skills_sh_skill_count=37 skills_sh_top_installs=5120",
    "search_rank": {"search_rank_skills_sh_leaderboard": 142},
    "duplicate_count": 1,
    "name_collision_count": 0,
    "name_shared_with": [],
    "locations": [
        {
            "owner": "affaan-m",
            "repo": "ECC",
            "path": "affaan-m/ECC/skills/e2e-testing/SKILL.md",
            "repo_url": "https://github.com/affaan-m/ECC",
            "skill_url": "https://github.com/affaan-m/ECC/blob/HEAD/skills/e2e-testing/SKILL.md",
            "sources": ["marketplace", "search"],
            "stars": 240095,
            "ranking": "skills_sh_rank=142 skills_sh_skill_count=37 skills_sh_top_installs=5120",
            "language": "en",
            "agent_compatibility": ["claude-code"],
            # The DETERMINISTIC Vettd scan rollup, written per-location by
            # publish_scans.py (_findings_summary); preserved across a re-index.
            "vettd_scan_findings": {
                "scan_id": "scn_01J9Z7Q3K8V2M4N6P8R0T2W4Y6",
                "overall_grade": "B",
                "trust_level": "cautious",
                "has_malicious_findings": False,
                "finding_count": 4,
                "severity_counts": {"critical": 0, "high": 0, "medium": 1, "low": 2, "info": 1},
                "categories_flagged": ["scripts", "best-practices"],
                "top_findings": [
                    {
                        "rule_id": "shell-exec-unpinned-install",
                        "category": "scripts",
                        "severity": "medium",
                        "label": "SKILL.md tells the agent to run 'npx playwright install' without a pinned version",
                    },
                    {
                        "rule_id": "network-egress-undeclared",
                        "category": "best-practices",
                        "severity": "low",
                        "label": "Downloads browser binaries from the Playwright CDN; egress not declared in frontmatter",
                    },
                ],
            },
        }
    ],
    "language": "en",
    "agent_compatibility": ["claude-code"],
    # Non-deterministic LLM threat scan (POST /scan/skill). model / prompt_version
    # / content_sha256 are from the E2E_TEST_PLAN_04 run; the finding is
    # representative of the shape (a real re-run may return NONE / 0).
    "llm_scan": {
        "model": "openrouter/deepseek/deepseek-v3.2",
        "prompt_version": "37243f9d5700",
        "scanned_at": "2026-08-30T21:41:26.512874+00:00",
        "content_sha256": "b0e00e7e17cb259101139900816c5528aed18dd10bcf5f9cb42cfc35baf8a755",
        "max_severity": "LOW",
        "finding_count": 1,
        "primary_threats": ["unpinned-dependency-install"],
        "overall_assessment": (
            "Benign testing skill. One low-severity note: instructs the agent to install "
            "the Playwright CLI globally at an unpinned version, which widens the supply-chain "
            "surface but is standard practice for this tool."
        ),
        "findings": [
            {
                "severity": "LOW",
                "aitech": "AITech-4.3",
                "aisubtech": "AITech-4.3.2",
                "title": "Unpinned global CLI install",
                "description": (
                    "SKILL.md instructs `npm install -g playwright` with no version constraint; "
                    "the agent will pull whatever is latest at run time."
                ),
                "location": "SKILL.md, 'Setup' section",
                "evidence": "npm install -g playwright && npx playwright install chromium",
                "remediation": "Pin the version, e.g. `npm install -g playwright@1.55.0`.",
            }
        ],
    },
    # CLI/dependency security scan (cli-security-scan/build_cli_export.py).
    "cli_security": {
        "grade": "C",
        "scanned_at": "2026-08-30T21:40:47.315190+00:00",
        "osv_snapshot_date": "2026-08-30",
        "packages": [
            {
                "package": "playwright",
                "ecosystem": "npm",
                "classification": "cli",
                "install_command": "npx playwright test tests/search.spec.ts --repeat-each=10",
                "vuln_count": 1,
                "max_severity": "HIGH",
                "advisory_ids": ["GHSA-7mvr-c777-76hp"],
            }
        ],
    },
}

_MCP_HIT_EXAMPLE: dict[str, Any] = {
    "score": 0.75,
    "rank": 1,
    "mcp_id": "github:upstash/context7",
    "name": "io.github.upstash/context7",
    "description": (
        "A Model Context Protocol server that fetches up-to-date, version-specific "
        "documentation and code examples from libraries directly into LLM prompts, "
        "helping developers get accurate answers without outdated or hallucinated information."
    ),
    "readme": "![Cover](...)\n\n# Context7 Platform - Up-to-date Code Docs For Any Prompt\n\n<full README markdown -- truncated in this example>",
    "repo_url": "https://github.com/upstash/context7",
    "status": "active",
    "mcp_category": "server",
    "mcp_category_source": "rule",
    "sources": ["repo_scan", "official_registry", "glama"],
    "registry_type": "npm",
    "package_identifier": "@upstash/context7-mcp",
    "package_url": "https://www.npmjs.com/package/@upstash/context7-mcp",
    "deployment": "hybrid",
    "transport": "stdio",
    "has_installable_package": True,
    "has_remote": True,
    "attributes": ["hosting:remote-capable"],
    "license": "MIT License",
    "added": "2026-08-17",
    "stars": 61421,
    "language": "TypeScript",
    "weekly_downloads": 867314,
    "monthly_downloads": 3729481,
    # OSV.dev via fetch_mcp_security.py. The npm package itself is clean
    # (security_vuln_count 0); the real signal is the direct-dependency pass:
    # 8 deps scanned, 44 advisories across zod / jose / undici / express, max HIGH.
    "security_source": "osv",
    "security_vuln_count": 0,
    "security_vuln_ids": [],
    "security_max_severity": None,
    "security_direct_deps_scanned": 8,
    "security_direct_deps_vuln_count": 44,
    "security_direct_deps_with_vulns": ["zod", "jose", "undici", "express"],
    "security_direct_deps_max_severity": "HIGH",
    "security_direct_deps_vuln_ids": [
        "GHSA-wqq4-5wpv-mx2g",
        "GHSA-m4v8-wqvr-p9f7",
        "GHSA-cxjh-pqwp-8mfp",
        "GHSA-qw6h-vgh9-j6wx",
        "GHSA-jj4c-3xjj-3xjr",
        "GHSA-9wv6-86v2-598j",
        "... (44 advisory ids total -- truncated in this example)",
    ],
}


class SkillHit(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _SKILL_HIT_EXAMPLE})

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
    # CLI/dependency security scan of the command-line tools this skill tells
    # you to install (cli-security-scan/build_cli_export.py; see
    # docs/ARCHITECTURE_CLI_SECURITY_SCAN.md). Null unless the skill installs a
    # confirmed-CLI package. {grade, packages[], scanned_at, osv_snapshot_date}.
    cli_security: dict[str, Any] | None


class McpHit(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": _MCP_HIT_EXAMPLE})

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
    transport: str | None
    has_installable_package: bool
    has_remote: bool
    attributes: tuple[str, ...]
    license: str | None
    added: str | None
    # Ranking signal from fetch_mcp_rankings.py (null if the row predates
    # that pass or has no resolvable repo/package).
    stars: int | None
    language: str | None
    weekly_downloads: int | None
    monthly_downloads: int | None
    # OSV.dev scan from fetch_mcp_security.py. security_source == "osv" with
    # security_vuln_count 0 means "scanned, nothing known"; all-null means
    # "never scanned". The security_direct_deps_* fields are the direct-
    # dependency pass: for a package that is itself clean but ships
    # vulnerable deps, security_vuln_count is 0 / security_max_severity is
    # null and the real signal is security_direct_deps_vuln_count /
    # security_direct_deps_max_severity.
    security_source: str | None
    security_vuln_count: int | None
    security_vuln_ids: tuple[str, ...] | None
    security_max_severity: str | None
    security_direct_deps_scanned: int | None
    security_direct_deps_vuln_count: int | None
    security_direct_deps_with_vulns: tuple[str, ...] | None
    security_direct_deps_max_severity: str | None
    security_direct_deps_vuln_ids: tuple[str, ...] | None


class QueryResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "index_ready": True,
                "query": "playwright end to end browser testing",
                "asset_type": "skill",
                "hits": [_SKILL_HIT_EXAMPLE],
            }
        }
    )

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
        cli_security=result.cli_security,
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
        transport=result.transport,
        has_installable_package=result.has_installable_package,
        has_remote=result.has_remote,
        attributes=result.attributes,
        license=result.license,
        added=result.added,
        stars=result.stars,
        language=result.language,
        weekly_downloads=result.weekly_downloads,
        monthly_downloads=result.monthly_downloads,
        security_source=result.security_source,
        security_vuln_count=result.security_vuln_count,
        security_vuln_ids=result.security_vuln_ids,
        security_max_severity=result.security_max_severity,
        security_direct_deps_scanned=result.security_direct_deps_scanned,
        security_direct_deps_vuln_count=result.security_direct_deps_vuln_count,
        security_direct_deps_with_vulns=result.security_direct_deps_with_vulns,
        security_direct_deps_max_severity=result.security_direct_deps_max_severity,
        security_direct_deps_vuln_ids=result.security_direct_deps_vuln_ids,
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
            min_stars=request.min_stars,
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
