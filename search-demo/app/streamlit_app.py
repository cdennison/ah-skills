from typing import TypedDict

import streamlit as st

from search import SearchResult, search_skills


class ResultRow(TypedDict):
    Rank: int
    Skill: str
    Repository: str
    Stars: int | None
    Match: float
    Security_Scan: str
    Description: str
    Path: str
    Sources: str
    Also_In: str
    Name_Collision: str


def to_result_row(result: SearchResult) -> ResultRow:
    extra_copies = result.duplicate_count - 1
    return ResultRow(
        Rank=result.rank,
        Skill=result.name,
        Repository=result.repository,
        Stars=result.stars,
        Match=result.score,
        Security_Scan=result.security_scan.value,
        Description=result.description,
        Path=result.path,
        Sources=", ".join(result.sources) if result.sources else "—",
        Also_In=f"+{extra_copies} more repo{'s' if extra_copies != 1 else ''}" if extra_copies > 0 else "—",
        Name_Collision=(
            f"{result.name_collision_count} other repo{'s' if result.name_collision_count != 1 else ''} use this name"
            if result.name_collision_count > 0
            else "—"
        ),
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
            "Stars",
            "Match",
            "Security_Scan",
            "Description",
            "Path",
            "Sources",
            "Also_In",
            "Name_Collision",
        ),
        column_config={
            "Rank": st.column_config.NumberColumn("#", width="small", format="%d"),
            "Skill": st.column_config.TextColumn("Skill", width="medium"),
            "Repository": st.column_config.TextColumn("Repository", width="small"),
            "Stars": st.column_config.NumberColumn("★ Stars", width="small", format="%d"),
            "Match": st.column_config.NumberColumn("Match", width="small", format="%.3f"),
            "Security_Scan": st.column_config.TextColumn("Security Scan", width="medium"),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "Path": st.column_config.TextColumn("Source path", width="large"),
            "Sources": st.column_config.TextColumn("Discovered via", width="medium"),
            "Also_In": st.column_config.TextColumn("Duplicate of", width="small"),
            "Name_Collision": st.column_config.TextColumn("Name also used by", width="medium"),
        },
    )


render_app()
