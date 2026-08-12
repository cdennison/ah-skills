import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from secrets import choice
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

ROOT_DIR: Final = Path(__file__).resolve().parent.parent
# SKILLS_QDRANT_URL lets a caller point at an alternate Qdrant server.
# Unset (the default) uses the Docker instance at localhost:6333.
QDRANT_URL: Final = os.environ.get("SKILLS_QDRANT_URL", "http://localhost:6333")
# SKILLS_QDRANT_DB_PATH lets a caller point at an alternate local *embedded*
# Qdrant store instead -- e.g. a smaller snapshot built by
# make_small_index.py -- without touching code. Unset (the default) uses
# the Docker server above.
_db_path_override = os.environ.get("SKILLS_QDRANT_DB_PATH")
DB_PATH: Final = Path(_db_path_override) if _db_path_override else None
COLLECTION: Final = "agent_skills"
MODEL_NAME: Final = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL_NAME: Final = "Qdrant/bm25"
DENSE_VECTOR_NAME: Final = "dense"
SPARSE_VECTOR_NAME: Final = "sparse"


class SecurityStatus(StrEnum):
    PASS = "Pass"
    WARN = "Warn"
    FAIL = "Fail"


SECURITY_STATUS_OPTIONS: Final = tuple(SecurityStatus)

# Opening a local on-disk QdrantClient against this collection costs ~110s
# once it grows past ~150k points (confirmed by direct timing: `open` took
# 111s, a 500-point `scroll` on the already-open client took 1.2s) -- the
# cost is almost entirely in the open, not per-query. Every call used to pay
# that cost independently by opening+closing its own client; share one
# lazily-created client for the life of the process instead (matches the
# `st.cache_resource` TODO already flagged in docs/QUERY_INTERFACE.md).
_client: QdrantClient | None = None


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(path=str(DB_PATH)) if DB_PATH else QdrantClient(url=QDRANT_URL)
    return _client


class SkillPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    repo: str
    name: str
    description: str = ""
    sources: tuple[str, ...] = ()
    stars: int | None = None
    duplicate_count: int = 1
    name_collision_count: int = 0
    name_shared_with: tuple[str, ...] = ()
    ranking: str = ""
    # Full-payload fields (see docs/QUERY_INTERFACE.md's payload table) --
    # not used by the Streamlit UI but needed to hand back the complete
    # payload shape documented in docs/NEXTJS_INTEGRATION.md.
    owner: str = ""
    repo_url: str = ""
    skill_url: str = ""
    content: str = ""
    content_hash: str = ""
    locations: tuple[dict, ...] = Field(default_factory=tuple)
    # Spoken/content language of the SKILL.md text (e.g. "en", "ja-JP",
    # "zh-CN"), NOT the source repo's programming language -- see
    # index_qdrant.py's _content_language().
    language: str = ""
    agent_compatibility: tuple[str, ...] = ()


# Mirrors export_csv.py's `_SEARCH_RANK_TOKEN_RE` -- both parse the same
# `search_rank_<source>_<metric>=N` tokens out of the flattened `ranking`
# payload string written by index_qdrant.py's `_ranking_string`. Keep these
# two regexes in sync; see docs/QUERY_INTERFACE.md's ranking metadata section.
_SEARCH_RANK_TOKEN_RE = re.compile(r"(?:^|\s)search_rank_(\S+?)=(\d+)")


def parse_search_rank(ranking: str) -> dict[str, int]:
    """Extract {metric_name: rank} from a flattened `ranking` payload string.
    Lower rank = better (rank 0 is the top result for that source/sort)."""
    return {name: int(value) for name, value in _SEARCH_RANK_TOKEN_RE.findall(ranking or "")}


@dataclass(frozen=True, slots=True)
class SearchResult:
    rank: int
    name: str
    repository: str
    score: float | None
    security_scan: SecurityStatus
    description: str
    path: str
    sources: tuple[str, ...]
    stars: int | None
    duplicate_count: int
    name_collision_count: int
    name_shared_with: tuple[str, ...]
    search_rank: dict[str, int]
    owner: str
    repo_url: str
    skill_url: str
    content: str
    ranking: str
    locations: tuple[dict, ...]
    language: str
    agent_compatibility: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Widget-driven filters for the Streamlit sidebar. All three are pushed
    down to Qdrant as native payload filters -- `rank_filters` (metric name
    -> "rank must be <= this value") relies on index_qdrant.py writing each
    search_rank_<source>_<metric> as a real top-level int payload field (see
    docs/QUERY_INTERFACE.md), so Qdrant applies it as part of the ANN search
    itself instead of discarding already-fetched results afterward."""

    min_stars: int | None = None
    sources: tuple[str, ...] = ()
    rank_filters: dict[str, int] = field(default_factory=dict)
    languages: tuple[str, ...] = ()
    agent_compatibility: tuple[str, ...] = ()

    def is_active(self) -> bool:
        return bool(
            self.min_stars
            or self.sources
            or self.rank_filters
            or self.languages
            or self.agent_compatibility
        )


def filters_to_qdrant_filter(filters: SearchFilters | None) -> models.Filter | None:
    if filters is None:
        return None
    conditions: list[models.Condition] = []
    if filters.min_stars:
        conditions.append(
            models.FieldCondition(key="stars", range=models.Range(gte=filters.min_stars))
        )
    if filters.sources:
        conditions.append(
            models.FieldCondition(key="sources", match=models.MatchAny(any=list(filters.sources)))
        )
    if filters.languages:
        conditions.append(
            models.FieldCondition(
                key="language", match=models.MatchAny(any=list(filters.languages))
            )
        )
    if filters.agent_compatibility:
        conditions.append(
            models.FieldCondition(
                key="agent_compatibility",
                match=models.MatchAny(any=list(filters.agent_compatibility)),
            )
        )
    for metric, max_rank in filters.rank_filters.items():
        conditions.append(models.FieldCondition(key=metric, range=models.Range(lte=max_rank)))
    return models.Filter(must=conditions) if conditions else None


StatusPicker = Callable[[tuple[SecurityStatus, ...]], SecurityStatus]


def pick_random_security_status(statuses: tuple[SecurityStatus, ...]) -> SecurityStatus:
    return choice(statuses)


def build_search_result(
    *, rank: int, payload: SkillPayload, score: float | None, security_scan: SecurityStatus
) -> SearchResult:
    return SearchResult(
        rank=rank,
        name=payload.name,
        repository=payload.repo,
        score=score,
        security_scan=security_scan,
        description=payload.description,
        path=payload.path,
        sources=payload.sources,
        stars=payload.stars,
        duplicate_count=payload.duplicate_count,
        name_collision_count=payload.name_collision_count,
        name_shared_with=payload.name_shared_with,
        search_rank=parse_search_rank(payload.ranking),
        owner=payload.owner,
        repo_url=payload.repo_url,
        skill_url=payload.skill_url,
        content=payload.content,
        ranking=payload.ranking,
        locations=payload.locations,
        language=payload.language,
        agent_compatibility=payload.agent_compatibility,
    )


def search_skills(
    query: str,
    *,
    limit: int = 12,
    filters: SearchFilters | None = None,
    status_picker: StatusPicker = pick_random_security_status,
) -> list[SearchResult]:
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
        build_search_result(
            rank=rank,
            payload=SkillPayload.model_validate(hit.payload),
            score=hit.score,
            security_scan=status_picker(SECURITY_STATUS_OPTIONS),
        )
        for rank, hit in enumerate(response.points, start=1)
    ]


def browse_skills(
    *,
    limit: int = 12,
    filters: SearchFilters,
    status_picker: StatusPicker = pick_random_security_status,
) -> list[SearchResult]:
    """Filter-only browsing for a blank query: no semantic/keyword ranking
    signal exists, so results are ordered by stars (repos with unknown star
    counts sort last) instead of a fabricated match score."""
    qdrant_filter = filters_to_qdrant_filter(filters)
    client = _get_client()
    candidates: list[SkillPayload] = []
    offset = None
    # Qdrant already applies scroll_filter server-side, so this is just
    # pagination -- fetch enough matching points to sort and truncate.
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
        candidates.extend(SkillPayload.model_validate(p.payload) for p in points)
        if offset is None:
            break

    candidates.sort(key=lambda p: (p.stars is None, -(p.stars or 0)))

    return [
        build_search_result(
            rank=rank,
            payload=payload,
            score=None,
            security_scan=status_picker(SECURITY_STATUS_OPTIONS),
        )
        for rank, payload in enumerate(candidates[:limit], start=1)
    ]


def discover_rank_metrics(*, sample_limit: int = 500) -> list[str]:
    """Union of real search_rank_* top-level payload keys seen across a
    sample of the collection, used to build filter widgets dynamically
    instead of hardcoding the known (query, sort) combos -- new ones show up
    on their own, matching export_csv.py's `search_rank_columns()`. Reading
    real payload keys (not re-parsing the `ranking` string) guarantees every
    metric reported here is actually usable in a native Qdrant FieldCondition."""
    client = _get_client()
    metrics: set[str] = set()
    points, _ = client.scroll(
        COLLECTION,
        with_payload=True,
        with_vectors=False,
        limit=sample_limit,
    )
    for point in points:
        metrics.update(k for k in (point.payload or {}) if k.startswith("search_rank_"))
    return sorted(metrics)
