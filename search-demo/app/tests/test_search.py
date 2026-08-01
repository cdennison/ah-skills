from search import SecurityStatus, SkillPayload, build_search_result, search_skills


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
