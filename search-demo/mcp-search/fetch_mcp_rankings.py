#!/usr/bin/env python3
"""Backfill ranking signal (GitHub stars, npm ranking) onto
mcp-repo-seeds/registry.json -- mcp_stats.py's ranking-coverage section
found this completely missing: 0% of the 82K-row registry carried any
popularity/ranking data. ../registry.py's skills pipeline already backfills
stars during clone_repos.py; this is the MCP-side equivalent, run as its own
standalone pass instead of tied to a clone step (this pipeline doesn't clone
repos at all -- see MCP_PIPELINE.md).

Two independent phases, either can be skipped (--stars-only /
--downloads-only):

1. GitHub stars -- for every row with a resolvable GitHub repo_url (~77.7K
   of 82K rows), GET api.github.com/repos/{owner}/{repo}.stargazers_count.
   Paced via shared.http.github_limiter() (4000/hr, shared with every other
   GitHub host this pipeline hits) using the same GITHUB_PAT as everything
   else (shared/github_auth.py) -- confirmed working (5000/hr ceiling from
   rate_limit_status()). At ~77.7K rows this is a ~19-20h run, same order of
   magnitude as run_overnight.sh's readme pass, and is meant to run the same
   way: unattended, overnight, resumable.

2. npm ranking -- for every row whose best package descriptor
   (export_mcp_csv.first_descriptor_value) has registry_type == "npm", GET
   registry.npmjs.org/-/v1/search?text=<package>. One call gets everything
   npm publicly exposes about a package's standing: weekly AND monthly
   downloads, a `dependents` count, and npm's own composite `score` (final,
   plus the quality/popularity/maintenance breakdown -- the same signal
   npms.io used to expose before it shut down; confirmed 404 directly, npm
   folded the scoring into its own search endpoint instead). This replaces
   an earlier version of this script that only hit
   api.npmjs.org/downloads/point/last-week/<package> for a single number --
   that endpoint is kept, but only as a fallback (see fetch_npm_point): the
   search endpoint is relevance-ranked, not an exact lookup, and a manual
   check against a random sample of 8 npm-typed rows in this registry found
   it missing the exact package ~25% of the time (2/8). No auth needed for
   either endpoint; paced via shared.http.default_limiter(). Only ~7.2K rows
   are npm-typed today, so this phase is comparatively quick.

   pypi downloads (~3K rows) are deliberately NOT fetched here -- a manual
   check against pypistats.org's public API returned 429 even on a single
   request with a real User-Agent from this environment. Wiring that up
   needs its own investigation with room to slow down further/retry, not
   baked into this run blind on an API that's already refusing traffic.

Resumable like pull_seed_repo.py: a row already refreshed within
--stale-days (default 7) is skipped by default; --rescan forces a full
re-fetch regardless of freshness. Progress is saved every SAVE_EVERY rows.
A 404 (repo gone / package unpublished) or other fetch error is recorded via
mcp_registry.record_error and skipped, not fatal to the run -- same
per-entry-isolation contract as every other bulk script in this pipeline
(see MCP_PIPELINE.md's "Spike" section for why that matters at this scale).

Deliberately registry.json-only: this script never touches qdrant. The
"download, then confirm" checkpoint is export_mcp_csv.py + mcp_stats.py
(both updated to surface stars/weekly_downloads), same as every other field
this pipeline gathers -- ranking data only reaches index_qdrant.py's payload
once a human has reviewed the CSV/stats and decided to run index_qdrant.py
(or its --rankings-only payload sync) by hand.

Meant to be run under supervise.sh, not invoked bare, since a ~20h run
outlives any one interactive session -- see that script's header for how to
(re)launch, check status, and stop it.

Usage:
    python fetch_mcp_rankings.py                  # full run, both phases
    python fetch_mcp_rankings.py --limit 50        # quick test
    python fetch_mcp_rankings.py --stars-only
    python fetch_mcp_rankings.py --downloads-only
    python fetch_mcp_rankings.py --rescan          # ignore --stale-days freshness check
"""

import argparse
import datetime
import sys
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from export_mcp_csv import first_descriptor_value
from shared.http import default_limiter, get_json, github_limiter

SAVE_EVERY = 200
DEFAULT_STALE_DAYS = 7
NPM_SEARCH_SIZE = 20  # how deep into npm's relevance-ranked search results to
# look for an exact package-name match before giving up on the richer
# fetch_npm_search() path and falling back to fetch_npm_point() -- see
# module docstring for the ~25%-miss-rate measurement behind this choice.


def is_stale(updated_iso: str | None, stale_days: int) -> bool:
    if not updated_iso:
        return True
    try:
        updated = datetime.datetime.fromisoformat(updated_iso)
    except ValueError:
        return True
    return (datetime.datetime.now() - updated) > datetime.timedelta(days=stale_days)


def fetch_stars(registry, index, limiter, *, limit, rescan, stale_days) -> None:
    candidates = []
    for r in registry:
        owner_repo = mcp_registry.parse_github_repo_url(r.get("repo_url") or "")
        if not owner_repo:
            continue
        if not rescan and not is_stale(r.get("stars_updated"), stale_days):
            continue
        candidates.append((r["id"], owner_repo))
    if limit is not None:
        candidates = candidates[:limit]

    print(f"[stars] {len(candidates)} row(s) to refresh")
    ok = failed = 0
    for i, (entry_id, (owner, repo)) in enumerate(candidates, start=1):
        try:
            data = get_json(f"https://api.github.com/repos/{owner}/{repo}", limiter)
            mcp_registry.set_stars(registry, entry_id, data.get("stargazers_count"), index=index)
            ok += 1
        except urllib.error.HTTPError as e:
            mcp_registry.record_error(registry, entry_id, "github_stars", f"{e.code} {e.reason}", index=index)
            failed += 1
        except Exception as e:
            mcp_registry.record_error(registry, entry_id, "github_stars", repr(e), index=index)
            failed += 1
        if i % SAVE_EVERY == 0:
            print(f"  [stars] {i}/{len(candidates)} ({ok} ok, {failed} failed) -- saving progress")
            mcp_registry.save_registry(registry)
    mcp_registry.save_registry(registry)
    print(f"[stars] done: {ok} ok, {failed} failed")


def fetch_npm_search(pkg: str, limiter) -> dict | None:
    """Richer path: npm's own search endpoint, one call, everything it
    publicly exposes about this package's standing. Returns None (not an
    error) if `pkg` isn't among the top NPM_SEARCH_SIZE relevance-ranked
    hits for its own name -- search is relevance-ranked, not an exact
    lookup, so a real miss here is expected and the caller falls back to
    fetch_npm_point()."""
    url = f"https://registry.npmjs.org/-/v1/search?text={urllib.parse.quote(pkg)}&size={NPM_SEARCH_SIZE}"
    data = get_json(url, limiter)
    for obj in data.get("objects", []):
        if (obj.get("package") or {}).get("name") != pkg:
            continue
        downloads = obj.get("downloads") or {}
        score = obj.get("score") or {}
        detail = score.get("detail") or {}
        ranking = {
            "weekly_downloads": downloads.get("weekly"),
            "monthly_downloads": downloads.get("monthly"),
            "npm_dependents": obj.get("dependents"),
            "npm_score_final": score.get("final"),
            "npm_score_quality": detail.get("quality"),
            "npm_score_popularity": detail.get("popularity"),
            "npm_score_maintenance": detail.get("maintenance"),
        }
        return {k: v for k, v in ranking.items() if v is not None}
    return None


def fetch_npm_point(pkg: str, limiter) -> dict:
    """Fallback for a package npm's own search doesn't surface for its exact
    name (see fetch_npm_search) -- this endpoint takes an exact package name
    directly, no search/ranking involved, so it always finds a published
    package. Weekly downloads only; no dependents/score available this way."""
    url = f"https://api.npmjs.org/downloads/point/last-week/{urllib.parse.quote(pkg, safe='')}"
    data = get_json(url, limiter)
    return {"weekly_downloads": data.get("downloads")}


def fetch_downloads(registry, index, limiter, *, limit, rescan, stale_days) -> None:
    candidates = []
    for r in registry:
        if first_descriptor_value(r, "registry_type") != "npm":
            continue
        pkg = first_descriptor_value(r, "package_identifier")
        if not pkg:
            continue
        if not rescan and not is_stale(r.get("downloads_updated"), stale_days):
            continue
        candidates.append((r["id"], pkg))
    if limit is not None:
        candidates = candidates[:limit]

    print(f"[downloads] {len(candidates)} row(s) to refresh")
    ok = via_search = via_point = failed = 0
    for i, (entry_id, pkg) in enumerate(candidates, start=1):
        try:
            ranking = fetch_npm_search(pkg, limiter)
            if ranking is not None:
                via_search += 1
            else:
                ranking = fetch_npm_point(pkg, limiter)
                via_point += 1
            mcp_registry.set_npm_ranking(registry, entry_id, ranking, index=index)
            ok += 1
        except urllib.error.HTTPError as e:
            mcp_registry.record_error(registry, entry_id, "npm_downloads", f"{e.code} {e.reason}", index=index)
            failed += 1
        except Exception as e:
            mcp_registry.record_error(registry, entry_id, "npm_downloads", repr(e), index=index)
            failed += 1
        if i % SAVE_EVERY == 0:
            print(
                f"  [downloads] {i}/{len(candidates)} ({ok} ok [{via_search} search, {via_point} point-fallback], "
                f"{failed} failed) -- saving progress"
            )
            mcp_registry.save_registry(registry)
    mcp_registry.save_registry(registry)
    print(f"[downloads] done: {ok} ok ({via_search} via search, {via_point} via point-fallback), {failed} failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Cap rows processed per phase (testing)")
    parser.add_argument("--stars-only", action="store_true")
    parser.add_argument("--downloads-only", action="store_true")
    parser.add_argument("--rescan", action="store_true", help="Refresh every eligible row regardless of freshness")
    parser.add_argument(
        "--stale-days", type=int, default=DEFAULT_STALE_DAYS,
        help=f"Skip rows refreshed within this many days unless --rescan (default {DEFAULT_STALE_DAYS})",
    )
    args = parser.parse_args()

    registry = mcp_registry.load_registry()
    index = mcp_registry.build_index(registry)

    if not args.downloads_only:
        fetch_stars(
            registry, index, github_limiter(), limit=args.limit, rescan=args.rescan, stale_days=args.stale_days
        )
    if not args.stars_only:
        fetch_downloads(
            registry, index, default_limiter(), limit=args.limit, rescan=args.rescan, stale_days=args.stale_days
        )


if __name__ == "__main__":
    main()
