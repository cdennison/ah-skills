from typing import TypedDict

import streamlit as st

from search import SearchResult, search_skills


class ResultRow(TypedDict):
    Rank: int
    Skill: str
    Repository: str
    Match: float
    Security_Scan: str
    Description: str
    Path: str


def to_result_row(result: SearchResult) -> ResultRow:
    return ResultRow(
        Rank=result.rank,
        Skill=result.name,
        Repository=result.repository,
        Match=result.score,
        Security_Scan=result.security_scan.value,
        Description=result.description,
        Path=result.path,
    )


def render_app() -> None:
    st.set_page_config(page_title="Agent skill search", page_icon="⌕", layout="wide")
    st.title("Agent skill search")
    st.caption("Hybrid semantic + keyword search over the local Qdrant index.")

    with st.form("skill_search"):
        input_column, button_column = st.columns([5, 1], vertical_alignment="bottom")
        with input_column:
            query = st.text_input(
                "Search skills",
                key="query",
                placeholder="Try: secure a Next.js app",
            )
        with button_column:
            submitted = st.form_submit_button(
                "Search",
                key="search",
                type="primary",
                width="stretch",
            )

    if not submitted:
        st.info("Enter a task, tool, or capability to search the local skill index.")
        return

    if not query.strip():
        st.warning("Enter a search query first.")
        return

    with st.spinner("Searching the local index…"):
        try:
            results = search_skills(query)
        except (OSError, RuntimeError, ValueError) as error:
            st.error(f"Could not search the local Qdrant index: {error}")
            return

    if not results:
        st.warning("No matching skills found. Try a broader query.")
        return

    header_column, hint_column = st.columns([3, 2], vertical_alignment="bottom")
    with header_column:
        st.subheader(f"{len(results)} results for “{query.strip()}”")
    with hint_column:
        st.caption("Click any column header to sort. Search again above for fresh results.")

    st.dataframe(
        [to_result_row(result) for result in results],
        width="stretch",
        height=480,
        hide_index=True,
        column_order=(
            "Rank",
            "Skill",
            "Repository",
            "Match",
            "Security_Scan",
            "Description",
            "Path",
        ),
        column_config={
            "Rank": st.column_config.NumberColumn("#", width="small", format="%d"),
            "Skill": st.column_config.TextColumn("Skill", width="medium"),
            "Repository": st.column_config.TextColumn("Repository", width="small"),
            "Match": st.column_config.NumberColumn("Match", width="small", format="%.3f"),
            "Security_Scan": st.column_config.TextColumn("Security Scan", width="medium"),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "Path": st.column_config.TextColumn("Source path", width="large"),
        },
    )


render_app()
