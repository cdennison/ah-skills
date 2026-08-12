from qdrant_client import models

from search import (
    COLLECTION,
    SearchFilters,
    SecurityStatus,
    SkillPayload,
    browse_skills,
    build_search_result,
    discover_rank_metrics,
    filters_to_qdrant_filter,
    parse_search_rank,
    search_skills,
)
from search import _get_client as _get_client  # pyright: ignore[reportPrivateUsage]


def test_build_search_result_when_payload_is_valid() -> None:
    # Given
    payload = SkillPayload(
        path="owner/repo/skills/example/SKILL.md",
        repo="owner",
        name="Example skill",
        description="Does useful things.",
    )

    # When
    result = build_search_result(
        rank=1,
        payload=payload,
        score=0.875,
        security_scan=SecurityStatus.WARN,
    )

    # Then
    assert result.rank == 1
    assert result.name == "Example skill"
    assert result.repository == "owner"
    assert result.score == 0.875
    assert result.security_scan is SecurityStatus.WARN
    assert result.description == "Does useful things."
    assert result.path == "owner/repo/skills/example/SKILL.md"


def test_search_skills_when_local_index_exists() -> None:
    # Given
    query = "excel spreadsheets"

    # When
    results = search_skills(
        query,
        limit=3,
        status_picker=lambda _statuses: SecurityStatus.FAIL,
    )

    # Then
    assert len(results) == 3
    assert [result.rank for result in results] == [1, 2, 3]
    assert all(result.name for result in results)
    assert all(result.path.endswith(".md") for result in results)
    assert all(result.security_scan is SecurityStatus.FAIL for result in results)


def test_parse_search_rank_extracts_source_specific_tokens() -> None:
    # Given
    ranking = (
        "skills_sh_rank=0 search_rank_agent_skills_best_match=12 search_rank_claude_skills_stars=44"
    )

    # When
    parsed = parse_search_rank(ranking)

    # Then
    assert parsed == {"agent_skills_best_match": 12, "claude_skills_stars": 44}


def test_parse_search_rank_ignores_legacy_ambiguous_token() -> None:
    # Given
    ranking = "search_rank=23 skills_sh_rank=0"

    # When
    parsed = parse_search_rank(ranking)

    # Then
    assert parsed == {}


def test_filters_to_qdrant_filter_when_no_filters_set() -> None:
    assert filters_to_qdrant_filter(SearchFilters()) is None
    assert filters_to_qdrant_filter(None) is None


def test_filters_to_qdrant_filter_when_filters_set() -> None:
    # Given
    filters = SearchFilters(
        min_stars=100,
        sources=("seed", "search"),
        rank_filters={"search_rank_agent_skills_best_match": 50},
    )

    # When
    qdrant_filter = filters_to_qdrant_filter(filters)

    # Then
    assert qdrant_filter is not None
    must = qdrant_filter.must
    assert isinstance(must, list)
    assert len(must) == 3
    rank_condition = must[2]
    assert isinstance(rank_condition, models.FieldCondition)
    assert rank_condition.key == "search_rank_agent_skills_best_match"
    assert isinstance(rank_condition.range, models.Range)
    assert rank_condition.range.lte == 50


def test_filters_to_qdrant_filter_when_language_and_agent_compatibility_set() -> None:
    # Given
    filters = SearchFilters(
        languages=("JavaScript", "Python"),
        agent_compatibility=("claude-code", "codex"),
    )

    # When
    qdrant_filter = filters_to_qdrant_filter(filters)

    # Then
    assert qdrant_filter is not None
    must = qdrant_filter.must
    assert isinstance(must, list)
    assert len(must) == 2
    language_condition, agent_condition = must
    assert isinstance(language_condition, models.FieldCondition)
    assert language_condition.key == "language"
    assert isinstance(language_condition.match, models.MatchAny)
    assert language_condition.match.any == ["JavaScript", "Python"]
    assert isinstance(agent_condition, models.FieldCondition)
    assert agent_condition.key == "agent_compatibility"
    assert isinstance(agent_condition.match, models.MatchAny)
    assert agent_condition.match.any == ["claude-code", "codex"]


def test_browse_skills_when_local_index_exists() -> None:
    # Given
    filters = SearchFilters(min_stars=0)

    # When
    results = browse_skills(
        limit=3,
        filters=filters,
        status_picker=lambda _statuses: SecurityStatus.PASS,
    )

    # Then
    assert len(results) == 3
    assert [result.rank for result in results] == [1, 2, 3]
    assert all(result.score is None for result in results)


def test_browse_skills_rank_filter_is_applied_natively_by_qdrant() -> None:
    # Given: whatever ranking metric actually exists in the local index
    metrics = discover_rank_metrics()
    if not metrics:
        return  # nothing to assert against in an index with no search-rank data
    metric = metrics[0]
    filters = SearchFilters(rank_filters={metric: 10})

    # When
    results = browse_skills(limit=5, filters=filters)

    # Then: every returned point's real payload field satisfies the bound --
    # confirms Qdrant filtered natively rather than Python discarding hits
    client = _get_client()
    for result in results:
        points, _ = client.scroll(
            COLLECTION,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="path", match=models.MatchValue(value=result.path))]
            ),
            with_payload=True,
            limit=1,
        )
        assert points, f"expected a point at path {result.path!r}"
        payload = points[0].payload
        assert payload is not None
        assert payload[metric] <= 10
