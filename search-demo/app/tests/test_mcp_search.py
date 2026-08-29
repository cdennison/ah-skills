from qdrant_client import models

from mcp_search import (
    McpPayload,
    McpSearchFilters,
    build_search_result,
    filters_to_qdrant_filter,
)
from query_service import McpHit, _to_mcp_hit  # pyright: ignore[reportPrivateUsage]


def _full_payload() -> McpPayload:
    return McpPayload(
        mcp_id="github:upstash/context7",
        name="io.github.upstash/context7",
        description="Up-to-date code docs for any prompt.",
        readme="# context7",
        repo_url="https://github.com/upstash/context7",
        registry_type="npm",
        package_identifier="@upstash/context7-mcp",
        deployment="hybrid",
        transport="stdio",
        has_installable_package=True,
        has_remote=True,
        attributes=("hosting:remote-capable",),
        stars=61021,
        language="TypeScript",
        weekly_downloads=553744,
        monthly_downloads=4108483,
        security_source="osv",
        security_vuln_count=0,
        security_vuln_ids=(),
        security_max_severity=None,
    )


def test_build_search_result_carries_ranking_and_security_fields() -> None:
    result = build_search_result(rank=1, payload=_full_payload(), score=0.9)

    assert result.stars == 61021
    assert result.language == "TypeScript"
    assert result.weekly_downloads == 553744
    assert result.monthly_downloads == 4108483
    assert result.transport == "stdio"
    assert result.security_source == "osv"
    assert result.security_vuln_count == 0
    assert result.security_vuln_ids == ()
    assert result.security_max_severity is None


def test_mcp_payload_defaults_when_row_never_enriched() -> None:
    payload = McpPayload(mcp_id="glama:closed-source-thing", name="Closed Source Thing")

    assert payload.stars is None
    assert payload.language is None
    assert payload.security_source is None
    assert payload.security_vuln_count is None
    assert payload.security_vuln_ids is None
    assert payload.transport is None


def test_to_mcp_hit_maps_new_fields() -> None:
    result = build_search_result(rank=2, payload=_full_payload(), score=0.5)

    hit = _to_mcp_hit(result)

    assert isinstance(hit, McpHit)
    assert hit.stars == 61021
    assert hit.language == "TypeScript"
    assert hit.weekly_downloads == 553744
    assert hit.monthly_downloads == 4108483
    assert hit.transport == "stdio"
    assert hit.security_source == "osv"
    assert hit.security_vuln_count == 0
    assert hit.security_vuln_ids == ()
    assert hit.security_max_severity is None


def test_to_mcp_hit_when_security_fields_present() -> None:
    payload = McpPayload(
        mcp_id="github:example/vulnerable",
        name="Vulnerable Server",
        security_source="osv",
        security_vuln_count=3,
        security_vuln_ids=("GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff", "CVE-2025-0001"),
        security_max_severity="HIGH",
    )

    hit = _to_mcp_hit(build_search_result(rank=1, payload=payload, score=None))

    assert hit.security_vuln_count == 3
    assert hit.security_max_severity == "HIGH"
    assert hit.security_vuln_ids == ("GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff", "CVE-2025-0001")


def test_filters_to_qdrant_filter_pushes_down_min_stars() -> None:
    qdrant_filter = filters_to_qdrant_filter(McpSearchFilters(min_stars=100))

    assert qdrant_filter is not None
    must = qdrant_filter.must
    assert isinstance(must, list)
    assert len(must) == 1
    condition = must[0]
    assert isinstance(condition, models.FieldCondition)
    assert condition.key == "stars"
    assert isinstance(condition.range, models.Range)
    assert condition.range.gte == 100


def test_min_stars_makes_filters_active() -> None:
    assert McpSearchFilters().is_active() is False
    assert McpSearchFilters(min_stars=1).is_active() is True
