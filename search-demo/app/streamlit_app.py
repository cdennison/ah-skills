from typing import TypedDict

import streamlit as st

from search import (
    SearchFilters,
    SearchResult,
    browse_skills,
    discover_rank_metrics,
    search_skills,
)

KNOWN_SOURCES = ("seed", "search", "manual", "marketplace")


class ResultRow(TypedDict):
    Rank: int
    Skill: str
    Repository: str
    Stars: int | None
    Match: str
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
        Match=f"{result.score:.3f}" if result.score is not None else "Browse",
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


def humanize_rank_metric(metric: str) -> str:
    """search_rank_agent_skills_best_match -> 'agent-skills search, best match'."""
    remainder = metric.removeprefix("search_rank_")
    for suffix, label in (("_best_match", "best match"), ("_stars", "stars")):
        if remainder.endswith(suffix):
            source = remainder.removesuffix(suffix).replace("_", "-")
            return f"{source} search, {label}"
    return remainder.replace("_", " ")


@st.cache_data(show_spinner=False)
def cached_rank_metrics() -> list[str]:
    try:
        return discover_rank_metrics()
    except (OSError, RuntimeError, ValueError):
        return []


_MIN_STARS_KEY = "filter_min_stars"
_SOURCES_KEY = "filter_sources"


def reset_filters() -> None:
    """Clear every filter widget's stored state so the next render starts
    from defaults -- lets a user drop straight back to plain free-text hybrid
    search with nothing left over from a previous filter session. Widget
    state persists across reruns independent of what render_filters_sidebar()
    returns, so this must clear st.session_state directly, not just skip
    building a SearchFilters for one pass."""
    st.session_state.pop(_MIN_STARS_KEY, None)
    st.session_state.pop(_SOURCES_KEY, None)
    for key in [k for k in st.session_state if k.startswith("rank_filter_")]:
        del st.session_state[key]


def render_filters_sidebar() -> SearchFilters:
    st.sidebar.header("Filters")
    st.sidebar.button(
        "Reset filters",
        on_click=reset_filters,
        help="Clear every filter below and search with free text alone.",
        width="stretch",
    )
    min_stars = st.sidebar.number_input(
        "Minimum stars",
        min_value=0,
        value=0,
        step=10,
        key=_MIN_STARS_KEY,
        help="Only show skills whose repository has at least this many GitHub stars.",
    )
    sources = st.sidebar.multiselect(
        "Discovered via",
        options=KNOWN_SOURCES,
        key=_SOURCES_KEY,
        help="Only show skills found through at least one of the selected discovery channels "
        "(seed list, GitHub code search, manually added, or a skills marketplace/registry).",
    )

    rank_filters: dict[str, int] = {}
    rank_metrics = cached_rank_metrics()
    if rank_metrics:
        with st.sidebar.expander("Ranking filters"):
            st.caption("Lower is better — rank 0 is the top result for that source's search.")
            for metric in rank_metrics:
                value = st.number_input(
                    humanize_rank_metric(metric),
                    min_value=0,
                    value=None,
                    placeholder="No limit",
                    key=f"rank_filter_{metric}",
                    help=f"Only show skills ranked this position or better ({metric}). "
                    "Skills with no ranking data for this metric are excluded once set.",
                )
                if value is not None:
                    rank_filters[metric] = int(value)

    return SearchFilters(
        min_stars=min_stars or None,
        sources=tuple(sources),
        rank_filters=rank_filters,
    )


def render_how_search_works() -> None:
    with st.expander("How search works"):
        st.markdown(
            "- **Hybrid search**: every query runs both a semantic (dense embedding) "
            "search and an exact keyword (BM25) search, then fuses the two rankings "
            "with Reciprocal Rank Fusion (RRF).\n"
            "- **Match** is that fused rank signal, not a percentage or cosine "
            "similarity — use it to compare results within one search, not across "
            "different searches.\n"
            "- **Stars** and **Discovered via** come straight from the repo's GitHub "
            "star count and which discovery channel(s) surfaced it (seed list, "
            "GitHub code search, manually added, or a skills marketplace/registry).\n"
            "- **Ranking filters** (sidebar) use each skill's rank position in GitHub "
            "code-search results for specific queries — lower is always better, and "
            "a skill with no data for a metric is excluded once that filter is set.\n"
            "- Leave the search box empty and set filters in the sidebar to **browse** "
            "by criteria alone; browsed results are ordered by stars since there's no "
            "query to rank them against."
        )


def render_app() -> None:
    st.set_page_config(page_title="Agent skill search", page_icon="⌕", layout="wide")
    st.title("Agent skill search")
    st.caption("Hybrid semantic + keyword search over the local Qdrant index.")
    render_how_search_works()

    filters = render_filters_sidebar()

    with st.form("skill_search"):
        input_column, button_column = st.columns([5, 1], vertical_alignment="bottom")
        with input_column:
            query = st.text_input(
                "Search skills",
                key="query",
                placeholder="Try: secure a Next.js app",
                help="Hybrid search: combines semantic similarity (dense embeddings) with "
                "exact keyword matching (BM25), fused by reciprocal rank. Leave blank and "
                "set filters in the sidebar to browse by criteria alone.",
            )
        with button_column:
            submitted = st.form_submit_button(
                "Search",
                key="search",
                type="primary",
                width="stretch",
            )

    if not submitted:
        st.info(
            "Enter a task, tool, or capability to search the local skill index, "
            "or set filters in the sidebar to browse."
        )
        return

    browsing = not query.strip()
    if browsing and not filters.is_active():
        st.warning("Enter a search query, or set at least one sidebar filter to browse.")
        return

    with st.spinner("Searching the local index…"):
        try:
            results = (
                browse_skills(filters=filters)
                if browsing
                else search_skills(query, filters=filters)
            )
        except (OSError, RuntimeError, ValueError) as error:
            st.error(f"Could not search the local Qdrant index: {error}")
            return

    if not results:
        st.warning("No matching skills found. Try a broader query or looser filters.")
        return

    header_column, hint_column = st.columns([3, 2], vertical_alignment="bottom")
    with header_column:
        heading = "results matching your filters" if browsing else f"results for “{query.strip()}”"
        st.subheader(f"{len(results)} {heading}")
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
            "Rank": st.column_config.NumberColumn(
                "#", width="small", format="%d", help="Position in this result set."
            ),
            "Skill": st.column_config.TextColumn(
                "Skill", width="medium", help="Skill name, from its SKILL.md frontmatter."
            ),
            "Repository": st.column_config.TextColumn(
                "Repository", width="small", help="GitHub repo that contains this skill."
            ),
            "Stars": st.column_config.NumberColumn(
                "★ Stars", width="small", format="%d", help="GitHub star count for the repository."
            ),
            "Match": st.column_config.TextColumn(
                "Match",
                width="small",
                help="Fused RRF rank score for this query (not a percentage or cosine similarity), "
                "or 'Browse' when results are filter-only with no search query.",
            ),
            "Security_Scan": st.column_config.TextColumn(
                "Security Scan", width="medium", help="Placeholder value — not a real scan result."
            ),
            "Description": st.column_config.TextColumn(
                "Description", width="large", help="Skill description from its frontmatter."
            ),
            "Path": st.column_config.TextColumn(
                "Source path", width="large", help="File path within the indexed source tree."
            ),
            "Sources": st.column_config.TextColumn(
                "Discovered via",
                width="medium",
                help="Discovery channel(s) that surfaced this repo.",
            ),
            "Also_In": st.column_config.TextColumn(
                "Duplicate of",
                width="small",
                help="Other repos hosting an identical copy of this skill.",
            ),
            "Name_Collision": st.column_config.TextColumn(
                "Name also used by",
                width="medium",
                help="Other repos with a different skill sharing this same name.",
            ),
        },
    )


if __name__ == "__main__":
    render_app()
