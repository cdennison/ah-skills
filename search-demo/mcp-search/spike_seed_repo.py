#!/usr/bin/env python3
"""Spike: pull the first N server entries out of the awesome-mcp-servers
seed repo's README and run scan_github_repo (no clone) against each,
printing results to console.

Purpose: sanity-check the seed-repo source end to end (README parsing ->
scan_mcp.py extraction) on a small sample before committing to full
vendoring/parsing machinery (source 6 in PROPOSED_PIPELINE.md). Not a
general-purpose seed parser -- deliberately narrow (first N list items
under the first category section) to keep the spike quick to read.

Usage:
    python spike_seed_repo.py
    python spike_seed_repo.py --count 5
"""

import argparse
import json
import re
import urllib.request

from scan_mcp import parse_github_repo_url, scan_github_repo

SEED_README_URL = "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/HEAD/README.md"
ENTRY_RE = re.compile(r"^- \[([^\]]+)\]\((https://github\.com/[^)\s]+)\)", re.MULTILINE)


def fetch_first_entries(count: int) -> list[tuple[str, str]]:
    """Return the first `count` (name, github_url) pairs from the first
    "Server Implementations" category section (Aggregators, as of writing)."""
    with urllib.request.urlopen(SEED_README_URL) as resp:
        text = resp.read().decode()

    section_start = text.find("## Server Implementations")
    if section_start == -1:
        raise ValueError("could not find '## Server Implementations' section in seed README")

    entries = ENTRY_RE.findall(text[section_start:])
    return entries[:count]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=3, help="How many seed entries to process (default 3)")
    args = parser.parse_args()

    entries = fetch_first_entries(args.count)
    print(f"pulled {len(entries)} entries from the seed repo:\n")
    for name, url in entries:
        print(f"  {name} -> {url}")
    print()

    ok = failed = 0
    for name, url in entries:
        print(f"=== {name} ({url}) ===")
        try:
            owner, repo = parse_github_repo_url(url)
            entry = scan_github_repo(owner, repo)
        except ValueError as e:
            print(f"  SKIPPED: {e}")
            failed += 1
            print()
            continue

        print(json.dumps(entry, indent=2))
        ok += 1
        print()

    print(f"result: {ok} extracted, {failed} skipped (no manifest / repo gone) out of {len(entries)}")


if __name__ == "__main__":
    main()
