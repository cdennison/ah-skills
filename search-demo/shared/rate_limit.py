"""Shared pacing helper for scripts that hit a rate-limited external API in
a loop (mcp-search's npm/GitHub fetchers today; the skills pipeline's own
clone_repos.py paces GitHub API calls with similar hand-rolled logic and
could migrate to this too).

Extracted because the identical "sleep `interval` seconds after processing
an item, unless it's the last one" check existed three times, unchanged,
across mcp-search/fetch_npm_mcp_candidates.py, backfill_readmes.py, and
classify_mcp.py before this existed.
"""

import collections
import time


class RateLimiter:
    """Enforces several simultaneous request-rate ceilings against one
    shared clock (e.g. 10/s AND 100/min AND 10000/hr all at once) -- for
    scripts hitting a public API for the first time with no established
    rate-limit guidance, where a single fixed interval doesn't express "stay
    under all of these caps at once." Call `.wait()` immediately before each
    HTTP request; it blocks only as long as needed to satisfy every window,
    then records the request.

    Used by mcp-search's pull_official_registry.py, pull_glama.py,
    pull_seed_repo.py, and download_readmes.py -- all new, unauthenticated
    hits against public APIs/raw-content hosts, hence the shared conservative
    default (see shared.http.DEFAULT_LIMITS).
    """

    def __init__(self, limits: dict[float, int]):
        # {window_seconds: max_requests_allowed_in_that_window}
        if not limits:
            raise ValueError("RateLimiter requires at least one window/limit pair")
        self._limits = dict(limits)
        self._widest_window = max(self._limits)
        self._history: collections.deque[float] = collections.deque()

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            while self._history and now - self._history[0] > self._widest_window:
                self._history.popleft()

            delay = 0.0
            for window, limit in self._limits.items():
                in_window = [t for t in self._history if now - t <= window]
                if len(in_window) >= limit:
                    delay = max(delay, window - (now - min(in_window)) + 0.01)
                # Smooth pacing, not just a hard ceiling: without this, a
                # loose window (e.g. 4000/3600s) does nothing at all until
                # the ceiling is hit, so a tighter window (e.g. 10/s) lets
                # requests burst through at close to its own rate --
                # burning through the loose window's whole budget in
                # minutes, then hard-blocking for what's left of that
                # window once the ceiling trips. Confirmed live: a
                # fetch_mcp_rankings.py run repeatedly burst through its
                # entire 4000/hr GitHub budget in ~15-20 minutes, then sat
                # completely idle (confirmed via /proc/<pid>/wchan showing
                # hrtimer_nanosleep, and GitHub's own rate_limit_status()
                # showing full, untouched quota during the "stall" -- i.e.
                # nothing was actually being throttled by GitHub, only by
                # this limiter's own bursty accounting) for 30-45+ minutes
                # per cycle, repeatedly, wasting the majority of the run's
                # wall-clock time. Enforcing a minimum gap of window/limit
                # between consecutive requests spreads usage evenly across
                # the window instead -- same total throughput allowed
                # (never exceeds the ceiling), but delivered as a steady
                # trickle rather than burst-then-block.
                if self._history:
                    min_interval = window / limit
                    since_last = now - self._history[-1]
                    if since_last < min_interval:
                        delay = max(delay, min_interval - since_last)

            if delay <= 0:
                break
            time.sleep(delay)

        self._history.append(time.monotonic())


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
