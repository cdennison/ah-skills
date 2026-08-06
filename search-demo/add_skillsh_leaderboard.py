#!/usr/bin/env python3
"""Copy GitHub repos out of a skills.sh leaderboard snapshot into
repo-seeds/registry.json as source="skills.sh".

Reads the already-saved leaderboard-raw/combined.json (produced by
pull_leaderboard.py -- see that file for why this script never pulls from
skills.sh itself). Each leaderboard entry is one *skill*, not one *repo*,
and the same repo routinely ships many skills (e.g. mattpocock/skills), so
entries are deduped down to one registry row per owner/repo before calling
registry.upsert() -- matching the "one registry row per repo" invariant
documented in registry.py.

Entries with sourceType != "github" (skills.sh's "well-known" sources, e.g.
the open.feishu.cn/lark-* skills) are skipped: there's no repo to clone.

Repos already tracked via another channel (seed/search/manual/marketplace)
are left alone except for a new "skills.sh" descriptor appended to their
existing `sources` list -- registry.upsert() already guarantees this, and
clone_repos.py already skips any repo that's already cloned on disk, so
running this script never triggers a re-clone of anything that's already
there.

This script only touches the registry -- it does not clone, extract, or
index anything itself. Run clone_repos.py / extract_search_raw.py /
index_qdrant.py afterward (see README.md).

Usage:
    python3 add_skillsh_leaderboard.py [leaderboard-raw/combined.json]
"""
import datetime
import json
import re
import sys
from pathlib import Path

import registry

REPO_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
DEFAULT_INPUT = Path(__file__).parent / "leaderboard-raw" / "combined.json"


def owner_repo_from_entry(entry: dict) -> tuple[str, str] | None:
    """Parse (owner, repo) out of a leaderboard entry, or None if it's not
    a GitHub-backed entry (e.g. skills.sh's "well-known" sourceType)."""
    if entry.get("sourceType") != "github":
        return None
    m = REPO_URL_RE.match(entry.get("installUrl") or "")
    if m:
        owner, repo = m.groups()
        return owner, repo.rstrip(".").removesuffix(".git")
    # Fall back to the "source" field (e.g. "owner/repo"), which is present
    # even when installUrl is missing.
    source = entry.get("source", "")
    if "/" in source:
        owner, repo = source.split("/", 1)
        return owner, repo
    return None


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not input_path.exists():
        print(
            f"[error] {input_path} not found. Pull a leaderboard snapshot by hand first:\n"
            f"  python3 pull_leaderboard.py 10000",
            file=sys.stderr,
        )
        sys.exit(1)

    entries = json.loads(input_path.read_text())
    print(f"Loaded {len(entries)} leaderboard entries from {input_path}")

    # Dedupe to one row per repo: keep the best (lowest/highest-install)
    # rank seen and a count of how many leaderboard skills came from it.
    by_repo: dict[tuple[str, str], dict] = {}
    skipped_non_github = 0
    for rank, entry in enumerate(entries):
        pair = owner_repo_from_entry(entry)
        if pair is None:
            skipped_non_github += 1
            continue
        key = (pair[0].lower(), pair[1].lower())
        agg = by_repo.setdefault(key, {
            "owner": pair[0],
            "repo": pair[1],
            "best_rank": rank,
            "skill_count": 0,
            "top_installs": entry.get("installs", 0),
        })
        agg["best_rank"] = min(agg["best_rank"], rank)
        agg["skill_count"] += 1
        agg["top_installs"] = max(agg["top_installs"], entry.get("installs", 0))

    print(f"Skipped {skipped_non_github} non-GitHub entries (e.g. skills.sh well-known sources)")
    print(f"{len(by_repo)} unique GitHub repos to upsert")

    registry_data = registry.load_registry()
    today = datetime.date.today().isoformat()
    new_count, overlap_count = 0, 0
    for agg in by_repo.values():
        existed = registry.find(registry_data, agg["owner"], agg["repo"]) is not None
        registry.upsert(registry_data, {
            "owner": agg["owner"],
            "repo": agg["repo"],
            "source": "skills.sh",
            "rank": agg["best_rank"],
            "skill_count": agg["skill_count"],
            "top_installs": agg["top_installs"],
            "rank_last_updated": today,
        })
        if existed:
            overlap_count += 1
        else:
            new_count += 1

    registry.save_registry(registry_data)
    print(f"Done: {new_count} new repos added, {overlap_count} already-tracked repos got a skills.sh descriptor added")
    print("Next: python3 clone_repos.py   (already-cloned repos are skipped automatically)")


if __name__ == "__main__":
    main()
