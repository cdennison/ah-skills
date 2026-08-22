#!/usr/bin/env python3
"""Run the clone -> extract -> optional scan publication -> index pipeline in fixed-size batches.

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

import publish_scans
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


def skill_directories_for_pairs(pairs):
    skill_dirs = []
    seen = set()
    for owner, repo in pairs:
        repo_dir = ROOT / "search-raw" / owner / repo
        if not repo_dir.is_dir():
            continue
        for skill_path in sorted(repo_dir.rglob("*")):
            if skill_path.is_file() and skill_path.name.lower() == "skill.md":
                skill_dir = skill_path.parent
                if skill_dir not in seen:
                    seen.add(skill_dir)
                    skill_dirs.append(skill_dir)
    return skill_dirs


def print_publish_summary(label, summary):
    print(
        f"[publish-scans] {label}: attempted={summary.attempted} "
        f"succeeded={summary.succeeded} skipped={summary.skipped} failed={summary.failed}"
    )
    for failure in summary.failures:
        print(f"[publish-scans] {label}: failed {failure.path}: {failure.message}", file=sys.stderr)


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
    parser.add_argument(
        "--publish-scans", action="store_true",
        help="Publish Vettd scans for current-batch extracted skills that are already indexed.",
    )
    args = parser.parse_args()

    prepared = None
    publish_config = None
    publish_attempted = 0
    publish_succeeded = 0
    publish_skipped = 0
    publish_failed = 0
    publish_indexed_batches = 0
    if args.publish_scans:
        try:
            publish_config = publish_scans.PublishConfig.from_env()
            prepared = publish_scans.preflight(publish_config)
        except (publish_scans.ConfigurationError, publish_scans.PreflightError) as error:
            print(f"[publish-scans] preflight failed: {error}", file=sys.stderr)
            sys.exit(2)

    try:
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

            if args.publish_scans and prepared is None:
                assert publish_config is not None
                try:
                    prepared = publish_scans.preflight(publish_config)
                except publish_scans.PreflightError as error:
                    print(f"[publish-scans] preflight failed: {error}", file=sys.stderr)
                    sys.exit(2)

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

            if not args.publish_scans:
                synced = mark_synced_pairs(confirmed)
                print(f"[mark-synced] {len(synced)}/{len(batch_pairs)} repos in this batch")

            if args.publish_scans and not args.skip_index:
                if prepared is not None:
                    prepared.client.close()
                    prepared = None
                run([sys.executable, "index_qdrant.py"])
                assert publish_config is not None
                try:
                    prepared = publish_scans.preflight(publish_config)
                except publish_scans.PreflightError as error:
                    print(f"[publish-scans] preflight failed: {error}", file=sys.stderr)
                    sys.exit(2)
                publish_indexed_batches += 1

            if args.publish_scans:
                assert prepared is not None
                summary = publish_scans.publish_skill_directories(
                    skill_directories_for_pairs(confirmed), prepared
                )
                print_publish_summary(f"batch {batch_num}", summary)
                publish_attempted += summary.attempted
                publish_succeeded += summary.succeeded
                publish_skipped += summary.skipped
                publish_failed += summary.failed
                failed_pairs = set()
                search_raw = (ROOT / "search-raw").resolve()
                for failure in summary.failures:
                    try:
                        failure_path = failure.path.resolve().relative_to(search_raw)
                    except ValueError:
                        continue
                    if len(failure_path.parts) >= 2:
                        failed_pairs.add((failure_path.parts[0], failure_path.parts[1]))
                synced = mark_synced_pairs([pair for pair in confirmed if pair not in failed_pairs])
                print(f"[mark-synced] {len(synced)}/{len(batch_pairs)} repos in this batch")

            if not args.keep_repos:
                run(["bash", str(CLEAN_REPOS_SCRIPT)])

            if args.stats:
                if not args.skip_index and not args.publish_scans:
                    if prepared is not None:
                        prepared.client.close()
                        prepared = None
                    run([sys.executable, "index_qdrant.py"])
                run_stats_to_log(batch_num)

            if not args.only_unsynced:
                offset += args.batch_size

        if not args.skip_index and not args.stats and (
            not args.publish_scans or publish_indexed_batches == 0
        ):
            if prepared is not None:
                prepared.client.close()
                prepared = None
            run([sys.executable, "index_qdrant.py"])

        if args.publish_scans:
            print(
                f"[publish-scans] final: attempted={publish_attempted} "
                f"succeeded={publish_succeeded} skipped={publish_skipped} failed={publish_failed}"
            )

        print("\nAll batches done.")
    finally:
        if prepared is not None:
            prepared.client.close()

    if publish_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
