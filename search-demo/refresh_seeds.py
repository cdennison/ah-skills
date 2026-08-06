#!/usr/bin/env python3
"""Re-vendor every seed list in repo-seeds/repo_seeds.json from its upstream
GitHub repo, and stamp last_pulled=now for each one refreshed.

A "seed list" (see registry.py's SEED_FILES / repo_seeds.json) is an
upstream awesome-list README vendored wholesale into repo-seeds/ and
regex-scraped for github.com links by registry.py's sync_seeds(). That
vendored copy goes stale the moment the upstream list adds a new repo --
until now there was no automated fetcher for it, only bookkeeping
(registry.py's `mark-seed-pulled` records that a refresh happened, it never
performed one). This script is the actual fetcher.

For each entry in repo-seeds/repo_seeds.json:
  1. shallow-clone `upstream_repo` (--depth 1, default branch) to a temp dir
  2. copy every SEED_FILES entry whose `name` matches into `vendored_path`
  3. mark_seed_pulled(name)

Run `registry.py sync-seed` afterward (or via RUN.sh, which chains the two)
to actually pick up any new repos the refreshed list contains -- this
script only refreshes the vendored copy on disk, it doesn't touch
registry.json itself.

Usage:
    ./refresh_seeds.py                    # refresh every seed list
    ./refresh_seeds.py officialskills.sh  # refresh just one
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import registry

ROOT = Path(__file__).parent


def refresh_seed(seed: dict) -> bool:
    name = seed["name"]
    upstream = seed["upstream_repo"]

    seed_file_entries = [sf for sf in registry.SEED_FILES if sf["name"] == name]
    if not seed_file_entries:
        print(f"[warn] {name!r} has no SEED_FILES entry in registry.py -- nothing to copy, skipping", file=sys.stderr)
        return False

    vendored_dir_name = Path(seed["vendored_path"]).name  # e.g. "awesome-agent-skills"

    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", upstream, tmp],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[error] could not clone {upstream}: {result.stderr.strip()}", file=sys.stderr)
            return False

        refreshed_any = False
        for sf in seed_file_entries:
            # sf["file"] is repo-seeds/-relative, e.g. "awesome-agent-skills/README.md".
            # Strip the vendored dir's own name to get the path within the fresh
            # clone (the vendored copy mirrors the upstream repo root 1:1).
            tail = Path(sf["file"]).relative_to(vendored_dir_name)
            src = Path(tmp) / tail
            dest = ROOT / "repo-seeds" / sf["file"]
            if not src.exists():
                print(f"[warn] {tail} not found in fresh clone of {upstream}, skipping", file=sys.stderr)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            print(f"Refreshed {dest.relative_to(ROOT)} from {upstream}")
            refreshed_any = True

    if refreshed_any:
        entry = registry.mark_seed_pulled(name)
        print(f"Marked {name} last_pulled={entry['last_pulled']}")
    return refreshed_any


def main():
    repo_seeds = registry.load_repo_seeds()
    if len(sys.argv) > 1:
        target = sys.argv[1]
        repo_seeds = [s for s in repo_seeds if s["name"] == target]
        if not repo_seeds:
            print(f"[error] {target!r} not found in repo-seeds/repo_seeds.json", file=sys.stderr)
            sys.exit(1)

    if not repo_seeds:
        print("No seed lists tracked in repo-seeds/repo_seeds.json -- nothing to do.")
        return

    any_failed = False
    for seed in repo_seeds:
        if not refresh_seed(seed):
            any_failed = True

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
