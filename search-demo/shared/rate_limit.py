"""Shared pacing helper for scripts that hit a rate-limited external API in
a loop (mcp-search's npm/GitHub fetchers today; the skills pipeline's own
clone_repos.py paces GitHub API calls with similar hand-rolled logic and
could migrate to this too).

Extracted because the identical "sleep `interval` seconds after processing
an item, unless it's the last one" check existed three times, unchanged,
across mcp-search/fetch_npm_mcp_candidates.py, backfill_readmes.py, and
classify_mcp.py before this existed.
"""

import time


def sleep_if_more(index: int, total: int, interval: float) -> None:
    """Sleep `interval` seconds, unless `index` (1-based) is the last of
    `total` -- i.e. call this right after processing item `index` in a
    `for index, item in enumerate(items, start=1)` loop to pace requests
    without an unnecessary trailing sleep after the final item. A plain
    function rather than a loop-wrapping generator so it still works
    correctly when some iterations skip the network call entirely (e.g.
    classify_mcp.py's --no-fetch cached branch, which shouldn't sleep at
    all) -- the caller decides per-iteration whether to call this, instead
    of a wrapper enforcing a sleep between every yield unconditionally."""
    if index < total:
        time.sleep(interval)
