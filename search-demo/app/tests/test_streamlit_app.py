from pathlib import Path
from typing import cast

from streamlit.testing.v1 import AppTest

from search import SecurityStatus, SkillPayload, build_search_result
from search import _get_client as _get_client  # pyright: ignore[reportPrivateUsage]
from streamlit_app import to_result_row

APP_PATH = Path(__file__).parents[1] / "streamlit_app.py"

# Opening a QdrantClient against this collection (150k+ points) costs 100s+
# -- confirmed by direct timing, almost entirely in the open, not per-query.
# search.py shares one client for the life of the process (_get_client()),
# but AppTest executes streamlit_app.py in-process, so its sidebar-driven
# discover_rank_metrics() call would otherwise pay that cost inline during
# script execution and can exceed even a generous AppTest timeout -- when
# that happens, AppTest's stop-timeout force-kills the running script thread
# mid-call, which can corrupt Streamlit's internal form-tracking state and
# surface as an unrelated "callbacks can only be defined on
# st.form_submit_button" error, not the real timeout. Warm the shared client
# once here, before any AppTest run, so the in-script call is instant.
_get_client()

_INITIAL_RENDER_TIMEOUT = 90
_POST_SUBMIT_TIMEOUT = 90


def test_search_flow_when_query_is_submitted() -> None:
    # Given
    app = AppTest.from_file(str(APP_PATH)).run(timeout=_INITIAL_RENDER_TIMEOUT)

    # When
    app.text_input(key="query").input("excel spreadsheets")
    app.button(key="search").click()
    app.run(timeout=_POST_SUBMIT_TIMEOUT)

    # Then
    assert not app.exception
    assert app.title[0].value == "Agent skill search"
    assert app.dataframe[0].value.shape[0] == 12
    assert "Security_Scan" in app.dataframe[0].value.columns


def test_browse_flow_when_only_filters_are_set() -> None:
    # Given
    app = AppTest.from_file(str(APP_PATH)).run(timeout=_INITIAL_RENDER_TIMEOUT)

    # When: leave the query blank but set a sidebar filter, then submit
    app.sidebar.number_input[0].set_value(100)
    app.button(key="search").click()
    app.run(timeout=_POST_SUBMIT_TIMEOUT)

    # Then
    assert not app.exception
    match_values = cast(
        "list[str]",
        app.dataframe[0].value["Match"].tolist(),  # pyright: ignore[reportUnknownMemberType]
    )
    assert match_values == ["Browse"] * len(match_values)


def test_to_result_row_shows_browse_for_filter_only_results() -> None:
    # Given
    payload = SkillPayload(
        path="owner/repo/skills/example/SKILL.md", repo="owner", name="Example skill"
    )
    result = build_search_result(
        rank=1, payload=payload, score=None, security_scan=SecurityStatus.PASS
    )

    # When
    row = to_result_row(result)

    # Then
    assert row["Match"] == "Browse"


def test_to_result_row_formats_numeric_match_score() -> None:
    # Given
    payload = SkillPayload(
        path="owner/repo/skills/example/SKILL.md", repo="owner", name="Example skill"
    )
    result = build_search_result(
        rank=1, payload=payload, score=0.8754321, security_scan=SecurityStatus.PASS
    )

    # When
    row = to_result_row(result)

    # Then
    assert row["Match"] == "0.875"
