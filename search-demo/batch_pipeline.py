#!/usr/bin/env python3
"""Run the clone -> extract -> index pipeline in fixed-size batches.

Keeps disk usage bounded: clones BATCH_SIZE repos, extracts their SKILL.md
files into search-raw/ (tiny, persists across batches), then deletes repos/
before moving on to the next batch. index_qdrant.py only ever reads from
search-raw/, so it's safe to run once at the end (or skip it and run it
separately).
"""

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

from registry import mark_synced_pairs, repo_pairs, unsynced_today

ROOT = Path(__file__).parent
CLEAN_REPOS_SCRIPT = ROOT / "clean_repos.sh"
STATS_LOG = ROOT / "stats.log"
CLONE_STATE_FILE = ROOT / ".clone_state.json"
MIN_FREE_BYTES = 1 * 1024 ** 3  # stop starting new batches once free space drops below this


def free_bytes():
    return shutil.disk_usage(ROOT).free


def load_clone_state():
    if CLONE_STATE_FILE.exists():
        return json.loads(CLONE_STATE_FILE.read_text())
    return {}


def run(cmd):
    print(f"[run] {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"[error] command failed with exit code {result.returncode}: {' '.join(cmd)}")
        sys.exit(result.returncode)


def run_stats_to_log(batch_num):
    result = subprocess.run(
        [sys.executable, "stats.py"], cwd=ROOT, capture_output=True, text=True,
    )
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    header = f"\n=== {timestamp} (batch {batch_num}) ===\n"
    print(header, end="")
    print(result.stdout)
    with open(STATS_LOG, "a") as f:
        f.write(header)
        f.write(result.stdout)
    if result.returncode != 0:
        print(f"[error] stats.py failed with exit code {result.returncode}")
        print(result.stderr)
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--source", metavar="TYPE", help="Passed through to clone_repos.py --source")
    parser.add_argument(
        "--start-offset", type=int, default=0,
        help="Resume a previous run by starting at this offset instead of 0.",
    )
    parser.add_argument(
        "--skip-index", action="store_true",
        help="Don't run index_qdrant.py at the end (e.g. to index manually or separately).",
    )
    parser.add_argument(
        "--keep-repos", action="store_true",
        help="Don't delete repos/ between batches (defeats the purpose, but useful for debugging).",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Run index_qdrant.py + stats.py after every batch instead of just at the end, "
        "so you can watch the counts climb batch by batch. Useful with a small --batch-size "
        "for debugging.",
    )
    parser.add_argument(
        "--only-unsynced", action="store_true",
        help="Passed through to clone_repos.py --only-unsynced: skip repos already synced "
        "today by content (registry.unsynced_today()) rather than by registry position, "
        "so batches don't waste time walking over already-synced repos interleaved "
        "throughout registry order.",
    )
    args = parser.parse_args()

    if args.only_unsynced:
        # --only-unsynced pool shrinks as mark_synced_pairs() runs after each
        # batch, so offset must stay 0 -- the front of the (shrinking) list is
        # always the next unprocessed slice. An incrementing offset would
        # skip repos as previously-processed ones drop out from under it.
        total = len(unsynced_today())
        print(f"Total unsynced-today repos in registry: {total}")
        if args.start_offset:
            print("[warn] --start-offset is ignored with --only-unsynced (offset always 0)")
    else:
        total = len(repo_pairs(source=args.source))
        print(f"Total repos in registry{f' (source={args.source!r})' if args.source else ''}: {total}")

    offset = 0 if args.only_unsynced else args.start_offset
    batch_num = 0
    while True:
        free = free_bytes()
        if free < MIN_FREE_BYTES:
            print(f"\n[disk] {free / 1024**3:.2f} GiB free, below the {MIN_FREE_BYTES / 1024**3:.0f} GiB "
                  "floor -- stopping before starting another batch so this run's already-cloned "
                  "data can finish processing and repos/ can be cleaned up.")
            break

        if args.only_unsynced:
            batch_pairs = [(r["owner"], r["repo"]) for r in unsynced_today()[:args.batch_size]]
            if not batch_pairs:
                break
        else:
            if offset >= total:
                break
            batch_pairs = repo_pairs(source=args.source)[offset:offset + args.batch_size]

        batch_num += 1
        print(f"\n=== Batch {batch_num}: {len(batch_pairs)} repos "
              f"({'unsynced pool' if args.only_unsynced else f'[{offset}:{offset + args.batch_size}) of {total}'}) ===")

        clone_offset = 0 if args.only_unsynced else offset
        clone_cmd = [sys.executable, "clone_repos.py", "--offset", str(clone_offset), str(args.batch_size)]
        if args.source:
            clone_cmd += ["--source", args.source]
        if args.only_unsynced:
            clone_cmd += ["--only-unsynced"]
        run(clone_cmd)

        run([sys.executable, "extract_search_raw.py"])

        # Only mark synced the repos actually confirmed cloned at some point
        # (present in .clone_state.json), not the whole batch -- clone_repos.py
        # already records failures itself via mark_sync_failure, and we must
        # not stamp last_synced on a repo that errored.
        clone_state = load_clone_state()
        confirmed = [pair for pair in batch_pairs if f"{pair[0]}/{pair[1]}" in clone_state]
        synced = mark_synced_pairs(confirmed)
        print(f"[mark-synced] {len(synced)}/{len(batch_pairs)} repos in this batch")

        if not args.keep_repos:
            run(["bash", str(CLEAN_REPOS_SCRIPT)])

        if args.stats:
            if not args.skip_index:
                run([sys.executable, "index_qdrant.py"])
            run_stats_to_log(batch_num)

        if not args.only_unsynced:
            offset += args.batch_size

    if not args.skip_index and not args.stats:
        run([sys.executable, "index_qdrant.py"])

    print("\nAll batches done.")


if __name__ == "__main__":
    main()
