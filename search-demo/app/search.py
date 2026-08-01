from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from secrets import choice
from typing import Final

from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient, models

ROOT_DIR: Final = Path(__file__).resolve().parent.parent
DB_PATH: Final = ROOT_DIR / "qdrant_db"
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


class SkillPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    repo: str
    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class SearchResult:
    rank: int
    name: str
    repository: str
    score: float
    security_scan: SecurityStatus
    description: str
    path: str


StatusPicker = Callable[[tuple[SecurityStatus, ...]], SecurityStatus]


def pick_random_security_status(statuses: tuple[SecurityStatus, ...]) -> SecurityStatus:
    return choice(statuses)


def build_search_result(
    *, rank: int, payload: SkillPayload, score: float, security_scan: SecurityStatus
) -> SearchResult:
    return SearchResult(
        rank=rank,
        name=payload.name,
        repository=payload.repo,
        score=score,
        security_scan=security_scan,
        description=payload.description,
        path=payload.path,
    )


def search_skills(
    query: str,
    *,
    limit: int = 12,
    status_picker: StatusPicker = pick_random_security_status,
) -> list[SearchResult]:
    normalized_query = query.strip()
    if not normalized_query:
        return []

    client = QdrantClient(path=str(DB_PATH))
    try:
        response = client.query_points(
            collection_name=COLLECTION,
            prefetch=[
                models.Prefetch(
                    query=models.Document(text=normalized_query, model=MODEL_NAME),
                    using=DENSE_VECTOR_NAME,
                    limit=max(limit, 20),
                ),
                models.Prefetch(
                    query=models.Document(text=normalized_query, model=SPARSE_MODEL_NAME),
                    using=SPARSE_VECTOR_NAME,
                    limit=max(limit, 20),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
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
    finally:
        client.close()
