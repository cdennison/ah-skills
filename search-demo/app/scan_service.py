"""Non-deterministic (LLM-backed) threat scan of a single skill-text blob.

Sends the supplied skill package text to an LLM with the Cisco skill-scanner
threat-analysis prompt and returns the model's structured verdict --
``findings`` / ``overall_assessment`` / ``primary_threats``, the same shape
``skill-scan-eval/scanner.py`` produces from a skill directory.

This module touches no Qdrant collection; it is orthogonal to the read-only
search path in ``query_service.py`` and only shares the FastAPI ``app``.

Provider: litellm, with OpenRouter as the initial provider. Configuration
(env vars, matching the Cisco skill-scanner where names overlap):

    OPENROUTER_API_KEY          OpenRouter key (litellm reads this natively)
    SKILL_SCANNER_LLM_API_KEY   optional; takes precedence over OPENROUTER_API_KEY
    SKILL_SCANNER_LLM_MODEL     optional litellm model id override
                                (default: openrouter/deepseek/deepseek-v3.2)

``prompts/skill_threat_analysis_prompt.md`` is a byte-for-byte mirror of the
repo-root ``prompts/skill_threat_analysis_prompt.md`` -- vendored so the app
stays self-contained for its Dockerfile. Keep them in sync.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "skill_threat_analysis_prompt.md"

DEFAULT_MODEL = "openrouter/deepseek/deepseek-v3.2"

# Upper bound on request payload size (~150k tokens of headroom). Kept as a
# module constant rather than env config to stay minimal.
MAX_SKILL_TEXT_CHARS = 600_000

# Seconds to wait on the upstream LLM call before giving up.
LLM_TIMEOUT_SECONDS = 120

Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

# Ascending order; index into this list ranks a finding's severity. "NONE" is
# the max_severity of an empty findings list (a clean verdict).
_SEVERITY_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
MaxSeverity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"]


@cache
def prompt_version() -> str:
    """Short digest of the threat-analysis prompt file, recorded on every
    verdict so a caller can tell which prompt produced it (and gate rescans
    on a prompt change). Cached -- the file does not change at runtime."""
    return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]


def max_severity(findings: list[ScanFinding]) -> MaxSeverity:
    ranks = [_SEVERITY_ORDER.index(f.severity) for f in findings]
    return _SEVERITY_ORDER[max(ranks)] if ranks else "NONE"

# Structured output schema matching the format the threat-analysis prompt
# describes. Mirrors RESPONSE_SCHEMA in skill-scan-eval/scanner.py.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "skill_threat_analysis",
        "schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                            },
                            "aitech": {"type": "string"},
                            "aisubtech": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "location": {"type": "string"},
                            "evidence": {"type": "string"},
                            "remediation": {"type": "string"},
                        },
                        "required": ["severity", "aitech", "title", "description"],
                    },
                },
                "overall_assessment": {"type": "string"},
                "primary_threats": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["findings", "overall_assessment", "primary_threats"],
        },
    },
}


class ScanConfigError(RuntimeError):
    """The scan endpoint is not configured (no API key available)."""


class ScanUpstreamError(RuntimeError):
    """The upstream LLM call failed or returned something unparseable."""


class ScanRequest(BaseModel):
    skill_text: str = Field(
        min_length=1,
        max_length=MAX_SKILL_TEXT_CHARS,
        description=(
            "Raw concatenated skill package text (SKILL.md plus any scripts). "
            "Internal layout is not interpreted; the '### FILE: <path>' "
            "convention from scanner.py is fine but not required."
        ),
    )
    skill_name: str = Field(
        default="",
        max_length=200,
        description="Optional label, interpolated into the analysis prompt only.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional per-request litellm model id override "
            f"(default: env SKILL_SCANNER_LLM_MODEL or '{DEFAULT_MODEL}')."
        ),
    )


class ScanFinding(BaseModel):
    severity: Severity
    aitech: str
    title: str
    description: str
    aisubtech: str | None = None
    location: str | None = None
    evidence: str | None = None
    remediation: str | None = None


class ScanResponse(BaseModel):
    model: str = Field(description="litellm model id the scan actually used.")
    prompt_version: str = Field(
        default_factory=prompt_version,
        description="Short digest of the threat-analysis prompt that produced this verdict.",
    )
    findings: list[ScanFinding]
    overall_assessment: str
    primary_threats: list[str]


def _resolve_model(override: str | None) -> str:
    return override or os.environ.get("SKILL_SCANNER_LLM_MODEL") or DEFAULT_MODEL


def _resolve_api_key() -> str:
    key = os.environ.get("SKILL_SCANNER_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ScanConfigError(
            "scan LLM not configured: set OPENROUTER_API_KEY (or SKILL_SCANNER_LLM_API_KEY)"
        )
    return key


def _complete(messages: list[dict[str, str]], model: str, api_key: str) -> str:
    """Thin seam around the provider call. ``litellm`` is imported lazily so
    this module (and the rest of the app) imports without it installed;
    tests monkeypatch this function."""
    import litellm

    # litellm.completion is only partially typed (ModelResponse vs stream
    # wrapper, Unknown-parameterised args); we always get a non-streamed
    # ModelResponse here and read it dynamically.
    response: Any = litellm.completion(  # pyright: ignore[reportUnknownMemberType]
        model=model,
        api_key=api_key,
        messages=messages,
        response_format=RESPONSE_SCHEMA,
        timeout=LLM_TIMEOUT_SECONDS,
    )
    content = response.choices[0].message.content
    return content if isinstance(content, str) else ""


def scan_skill_text(
    skill_text: str, skill_name: str = "", model: str | None = None
) -> ScanResponse:
    resolved_model = _resolve_model(model)
    api_key = _resolve_api_key()

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    label = skill_name.strip() or "unnamed"
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Analyze the following Agent Skill package (name: {label}):\n\n{skill_text}"
            ),
        },
    ]

    try:
        raw = _complete(messages, resolved_model, api_key)
    except ScanConfigError:
        raise
    except Exception as exc:  # any provider failure collapses to one upstream error
        raise ScanUpstreamError(f"scan LLM call failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ScanUpstreamError(f"scan LLM returned non-JSON output: {exc}") from exc

    try:
        return ScanResponse(model=resolved_model, **parsed)
    except (TypeError, ValueError) as exc:
        raise ScanUpstreamError(f"scan LLM output did not match schema: {exc}") from exc
