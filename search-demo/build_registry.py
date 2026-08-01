#!/usr/bin/env python3
"""One-time migration: build repo-seeds/registry.json from the pre-registry
inputs (awesome-agent-skills/README.md + MANUAL_REPOS.md).

After this runs once, registry.json is the single source of truth that
clone_repos.py reads -- README.md and MANUAL_REPOS.md are kept only as
human-readable/historical references and are no longer parsed by the
pipeline. New repos should be added via registry.py, not by editing this
script or re-running it (re-running would only pick up brand-new README/
MANUAL_REPOS.md entries; it won't touch existing registry rows).
"""

import re
import sys
from pathlib import Path

from registry import REGISTRY_FILE, load_registry, save_registry, upsert

README = Path(__file__).parent / "repo-seeds" / "awesome-agent-skills" / "README.md"
MANUAL_REPOS_FILE = Path(__file__).parent / "repo-seeds" / "MANUAL_REPOS.md"
REPO_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")

TODO_NOTE = "TODO: backfill reason -- migrated from MANUAL_REPOS.md 'Hand-picked' section, no original note recorded"


def seed_entries():
    text = README.read_text()
    seen = set()
    for owner, repo in REPO_URL_RE.findall(text):
        repo = repo.rstrip(".")
        key = f"{owner}/{repo}"
        if key in seen:
            continue
        seen.add(key)
        yield {
            "owner": owner,
            "repo": repo,
            "source": "seed",
            "file": "awesome-agent-skills/README.md",
        }


def manual_hand_picked_entries():
    if not MANUAL_REPOS_FILE.exists():
        return
    text = MANUAL_REPOS_FILE.read_text()
    section = text.split("## Hand-picked", 1)
    if len(section) < 2:
        return
    body = section[1].split("\n## ", 1)[0]
    for owner, repo in REPO_URL_RE.findall(body):
        yield {
            "owner": owner,
            "repo": repo.rstrip("."),
            "source": "manual",
            "note": TODO_NOTE,
        }


def manual_search_entries():
    """Parse '## From GitHub search (query: "...", reviewed YYYY-MM-DD)' sections."""
    if not MANUAL_REPOS_FILE.exists():
        return
    text = MANUAL_REPOS_FILE.read_text()
    heading_re = re.compile(
        r'## From GitHub search \(query: "([^"]+)", reviewed (\d{4}-\d{2}-\d{2})\)\n(.*?)(?=\n## |\Z)',
        re.DOTALL,
    )
    for query, reviewed_date, body in heading_re.findall(text):
        for owner, repo in REPO_URL_RE.findall(body):
            yield {
                "owner": owner,
                "repo": repo.rstrip("."),
                "source": "search",
                "query": query,
                "sort": "best-match",
                "exact": True,
                "reviewed_date": reviewed_date,
            }


def main():
    if REGISTRY_FILE.exists():
        print(f"[error] {REGISTRY_FILE} already exists -- this migration is one-time only.", file=sys.stderr)
        print("        Use registry.py to add new entries instead of re-running this script.", file=sys.stderr)
        sys.exit(1)

    registry = load_registry()

    counts = {"seed": 0, "manual": 0, "search": 0}
    for entry in seed_entries():
        upsert(registry, entry)
        counts["seed"] += 1
    for entry in manual_search_entries():
        upsert(registry, entry)
        counts["search"] += 1
    for entry in manual_hand_picked_entries():
        # Search entries win if a repo appears in both sections (shouldn't happen, but be defensive).
        if not any(r["owner"] == entry["owner"] and r["repo"] == entry["repo"] for r in registry):
            upsert(registry, entry)
            counts["manual"] += 1

    save_registry(registry)
    print(f"Built {REGISTRY_FILE} with {len(registry)} repos "
          f"(seed={counts['seed']}, manual={counts['manual']}, search={counts['search']})")
    todo_count = sum(1 for r in registry for s in r.get("sources", []) if s.get("note") == TODO_NOTE)
    if todo_count:
        print(f"[note] {todo_count} manual entries need a real note backfilled (see TODO markers in {REGISTRY_FILE})")


if __name__ == "__main__":
    main()
