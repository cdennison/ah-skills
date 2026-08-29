#!/usr/bin/env python3
"""Run scan_mcp.py's manifest extraction over rows ALREADY in
mcp-repo-seeds/registry.json and merge the result back onto each row --
closing the gap where scan_mcp cleanly extracts registry_type /
package_identifier / deployment / transport / remote_urls from a server's
own GitHub repo but nothing ever writes that onto a Glama- or
official-registry-sourced row (pull_seed_repo.py does this merge, but ONLY
for the punkpeye/awesome-mcp-servers seed list it vendors).

For each targeted row this fetches server.json, else package.json, else
pyproject.toml from the row's GitHub repo (raw.githubusercontent.com, no
clone -- same Fetcher path pull_seed_repo.py uses) and merges the extracted
fields via mcp_registry.merge_repo_scan():
  - ADDITIVE: a null/empty extracted value never overwrites an existing one.
  - PROVENANCE-STAMPED: fields land on a `repo_scan` source descriptor;
    the row gets `repo_scan_source: "scan_mcp"` + a `repo_scan_updated`
    clock (same shape as fetch_mcp_rankings.py / fetch_mcp_security.py).

Per-row isolation: a repo with no recognizable manifest raises
`ValueError` inside scan_mcp -- caught here at the row boundary, recorded
via mcp_registry.record_error, and the run continues (the exact
batch-killer MCP_PIPELINE.md's spike section flagged).

registry.json-only: never touches Qdrant. Re-index the affected rows
afterwards with `index_qdrant.py --ids ...` once the registry diff is
reviewed, same as every other enrichment pass in this pipeline.

Usage:
    python enrich_from_repo_scan.py --ids github:upstash/context7          # targeted (always rescans exactly these)
    python enrich_from_repo_scan.py --ids github:a/b,github:c/d
    python enrich_from_repo_scan.py --limit 50                              # first 50 stale/never-scanned rows
    python enrich_from_repo_scan.py --random-sample 20                      # 20 random eligible rows (review before scaling up)
    python enrich_from_repo_scan.py --rescan                               # re-attempt every resolvable-repo row regardless of freshness
"""

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from pull_seed_repo import rate_limited_github_fetcher
from scan_mcp import scan_entry
from shared.http import github_limiter

SAVE_EVERY = 100
DEFAULT_STALE_DAYS = 30  # a manifest changes far less often than a star
# count -- match fetch_mcp_security.py's window rather than
# fetch_mcp_rankings.py's 7 days.


def _is_stale(updated_iso: str | None, stale_days: int) -> bool:
    import datetime

    if not updated_iso:
        return True
    try:
        updated = datetime.datetime.fromisoformat(updated_iso)
    except ValueError:
        return True
    return (datetime.datetime.now() - updated) > datetime.timedelta(days=stale_days)


def enrich(registry, index, limiter, *, limit, random_sample, rescan, stale_days, only_ids=None) -> None:
    candidates = []
    for r in registry:
        if only_ids is not None and r["id"] not in only_ids:
            continue
        owner_repo = mcp_registry.parse_github_repo_url(r.get("repo_url") or "")
        if not owner_repo:
            continue  # source-scoped id (closed-source remote-only server) -- nothing to scan
        if only_ids is None and not rescan and not _is_stale(r.get("repo_scan_updated"), stale_days):
            continue
        candidates.append((r["id"], owner_repo))

    if random_sample is not None:
        candidates = random.sample(candidates, min(random_sample, len(candidates)))
    elif limit is not None:
        candidates = candidates[:limit]

    print(f"[repo-scan] {len(candidates)} row(s) to scan")
    ok = skipped = failed = 0
    for i, (entry_id, (owner, repo)) in enumerate(candidates, start=1):
        fetch = rate_limited_github_fetcher(owner, repo, limiter)
        try:
            extracted = scan_entry(fetch, f"{owner}/{repo}")
            mcp_registry.merge_repo_scan(registry, entry_id, extracted, index=index)
            ok += 1
            status = f"ok ({extracted.get('source_file')} -> {extracted.get('registry_type')})"
        except ValueError as e:
            # No recognizable manifest / no derivable name -- a real,
            # recordable fact about this repo, not a run-ending error.
            mcp_registry.record_error(registry, entry_id, "repo_scan", repr(e), index=index)
            skipped += 1
            status = f"skip ({e})"
        except Exception as e:
            mcp_registry.record_error(registry, entry_id, "repo_scan", repr(e), index=index)
            failed += 1
            status = f"FAILED ({e!r})"

        print(f"[{i}/{len(candidates)}] {owner}/{repo}: {status}")
        if i % SAVE_EVERY == 0:
            mcp_registry.save_registry(registry)

    mcp_registry.save_registry(registry)
    print(f"[repo-scan] done: {ok} merged, {skipped} skipped (no manifest), {failed} failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Cap rows scanned (testing)")
    parser.add_argument(
        "--random-sample", type=int, default=None, metavar="N",
        help="Scan N randomly chosen eligible rows instead of the full/limited set.",
    )
    parser.add_argument("--rescan", action="store_true", help="Re-attempt every resolvable-repo row regardless of freshness")
    parser.add_argument(
        "--stale-days", type=int, default=DEFAULT_STALE_DAYS,
        help=f"Skip rows scanned within this many days unless --rescan (default {DEFAULT_STALE_DAYS})",
    )
    parser.add_argument(
        "--ids", type=str, default=None,
        help="Comma-separated registry ids to scan (ignores freshness gating, always rescans exactly these rows).",
    )
    args = parser.parse_args()

    registry = mcp_registry.load_registry()
    index = mcp_registry.build_index(registry)
    only_ids = set(args.ids.split(",")) if args.ids else None

    enrich(
        registry, index, github_limiter(),
        limit=args.limit, random_sample=args.random_sample, rescan=args.rescan,
        stale_days=args.stale_days, only_ids=only_ids,
    )


if __name__ == "__main__":
    main()
