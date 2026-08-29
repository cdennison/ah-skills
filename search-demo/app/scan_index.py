"""Scan an indexed skill and record the verdict on its Qdrant point.

This is the one write path in the query service. Given a skill's Qdrant point
id (or content hash), it:

  1. reads the point's ``content`` (the SKILL.md text) from ``agent_skills``,
  2. decides whether a rescan is needed (rescan gate, below),
  3. runs the non-deterministic LLM threat scan (``scan_service``),
  4. writes a single top-level ``llm_scan`` payload key back onto that point
     via ``set_payload`` -- never a vector, never a new collection,
  5. returns the verdict.

Keeping the scan *and* the Qdrant write behind one call means every caller
(the pipeline step, the Next.js app, the smoke test) gets the same
"scan this skill and record it" primitive, with model/prompt/schema/gating
logic in exactly one place. The pure ``POST /scan`` (text in, verdict out,
no Qdrant) stays available for the eval harness and ad-hoc use.

Rescan gate (mirrors publish_scans.py's Vettd algorithm): skip when the
point already has an ``llm_scan`` whose ``content_sha256`` matches the
point's current content AND ``model`` + ``prompt_version`` are unchanged AND
it is younger than ``SCAN_RESCAN_INTERVAL_DAYS`` (default 7). ``force=True``
bypasses. Never skip on age alone -- the scan is non-deterministic.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import BaseModel, Field, model_validator
from qdrant_client import QdrantClient, models

import search
from scan_service import (
    DEFAULT_MODEL,
    MaxSeverity,
    ScanConfigError,
    ScanFinding,
    ScanUpstreamError,
    max_severity,
    prompt_version,
    scan_skill_text,
)

COLLECTION = search.COLLECTION


def _rescan_interval_days() -> int:
    raw = os.environ.get("SCAN_RESCAN_INTERVAL_DAYS", "7")
    try:
        value = int(raw)
    except ValueError:
        return 7
    return value if value >= 0 else 7


class SkillNotFound(RuntimeError):
    """No point in agent_skills matched the given point_id / content_hash."""


class ScanSkillRequest(BaseModel):
    point_id: str | None = Field(
        default=None, description="Qdrant point id of the skill in agent_skills."
    )
    content_hash: str | None = Field(
        default=None,
        description="Alternative lookup: the skill's content_hash payload value.",
    )
    model: str | None = Field(
        default=None, description="Optional litellm model id override for the scan."
    )
    force: bool = Field(
        default=False, description="Rescan even if a recent matching llm_scan exists."
    )

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> ScanSkillRequest:
        if bool(self.point_id) == bool(self.content_hash):
            raise ValueError("provide exactly one of point_id / content_hash")
        return self


class LlmScan(BaseModel):
    """The `llm_scan` payload field written onto the Qdrant point. Latest
    verdict only -- no history."""

    model: str
    prompt_version: str
    scanned_at: str
    content_sha256: str
    max_severity: MaxSeverity
    finding_count: int
    primary_threats: list[str]
    overall_assessment: str
    findings: list[ScanFinding]


class ScanSkillResponse(BaseModel):
    point_id: str
    skipped: bool
    reason: str | None = None
    llm_scan: LlmScan


def _find_point(
    client: QdrantClient, request: ScanSkillRequest
) -> tuple[str, dict[str, object]]:
    fields = ["content", "name", "content_hash", "llm_scan"]
    if request.point_id is not None:
        points = client.retrieve(
            COLLECTION, ids=[request.point_id], with_payload=fields, with_vectors=False
        )
        if not points:
            raise SkillNotFound(f"no point with id {request.point_id!r}")
        return str(points[0].id), dict(points[0].payload or {})

    matched, _ = client.scroll(
        COLLECTION,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="content_hash",
                    match=models.MatchValue(value=request.content_hash or ""),
                )
            ]
        ),
        with_payload=fields,
        with_vectors=False,
        limit=1,
    )
    if not matched:
        raise SkillNotFound(f"no point with content_hash {request.content_hash!r}")
    return str(matched[0].id), dict(matched[0].payload or {})


def _still_fresh(existing: dict[str, object], content_sha256: str, model: str) -> bool:
    if (
        existing.get("content_sha256") != content_sha256
        or existing.get("model") != model
        or existing.get("prompt_version") != prompt_version()
    ):
        return False
    scanned_at = existing.get("scanned_at")
    if not isinstance(scanned_at, str):
        return False
    try:
        when = datetime.fromisoformat(scanned_at)
    except ValueError:
        return False
    return datetime.now(UTC) - when < timedelta(days=_rescan_interval_days())


def scan_and_record(
    request: ScanSkillRequest, client: QdrantClient | None = None
) -> ScanSkillResponse:
    """Scan the identified skill and write its verdict to the Qdrant point.

    Raises SkillNotFound (-> 404), ScanConfigError (-> 503), ScanUpstreamError
    (-> 502); the FastAPI route maps those to status codes.
    """
    client = client or search._get_client()
    point_id, payload = _find_point(client, request)

    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SkillNotFound(f"point {point_id!r} has no content to scan")
    name = payload.get("name")
    name = name if isinstance(name, str) else ""
    content_sha256 = hashlib.sha256(content.encode()).hexdigest()
    # Same precedence as scan_service._resolve_model, so the rescan gate
    # compares against the model the scan will actually use.
    resolved_model = request.model or os.environ.get("SKILL_SCANNER_LLM_MODEL") or DEFAULT_MODEL

    existing = payload.get("llm_scan")
    if not request.force and isinstance(existing, dict):
        existing_scan = cast("dict[str, object]", existing)
        if _still_fresh(existing_scan, content_sha256, resolved_model):
            return ScanSkillResponse(
                point_id=point_id,
                skipped=True,
                reason="content, model and prompt unchanged since the last scan",
                llm_scan=LlmScan.model_validate(existing_scan),
            )

    verdict = scan_skill_text(content, name, request.model)
    llm_scan = LlmScan(
        model=verdict.model,
        prompt_version=verdict.prompt_version,
        scanned_at=datetime.now(UTC).isoformat(),
        content_sha256=content_sha256,
        max_severity=max_severity(verdict.findings),
        finding_count=len(verdict.findings),
        primary_threats=verdict.primary_threats,
        overall_assessment=verdict.overall_assessment,
        findings=verdict.findings,
    )
    client.set_payload(
        COLLECTION, payload={"llm_scan": llm_scan.model_dump(mode="json")}, points=[point_id]
    )
    return ScanSkillResponse(point_id=point_id, skipped=False, llm_scan=llm_scan)


__all__ = [
    "LlmScan",
    "ScanConfigError",
    "ScanSkillRequest",
    "ScanSkillResponse",
    "ScanUpstreamError",
    "SkillNotFound",
    "scan_and_record",
]
