#!/usr/bin/env bash
# RUN.sh -- full end-to-end pipeline in one command: repo discovery ->
# registry -> batched clone/extract/index -> CSV export.
#
# Steps (in order):
#   1. refresh_seeds.py             -- re-vendors every seed list (e.g. the
#                                       officialskills.sh awesome-list) from its
#                                       upstream repo. THIS MATTERS: sync-seed
#                                       (step 2) only scrapes github.com links out
#                                       of the vendored copy already on disk under
#                                       repo-seeds/ -- if that copy is stale, new
#                                       repos the upstream list has added are
#                                       invisible to sync-seed no matter how often
#                                       you run it. See "Every source needs its
#                                       own refresh" below.
#   2. registry.py sync-seed        -- additive, pick up new awesome-list repos
#                                       from the copy refresh_seeds.py just updated
#   3. fetch_marketplace.py         -- additive, pick up new marketplace repos
#   4. pull_leaderboard.py          -- COMMENTED OUT below, never actually run by
#                                       this script. Left in as a reference/reminder
#                                       of where it *would* slot in -- see the
#                                       "NEVER AUTOMATE" block right before step 5.
#   5. add_skillsh_leaderboard.py   -- OPTIONAL (--with-leaderboard): upserts rank
#                                       data from an EXISTING leaderboard-raw/ snapshot
#   6. search_github.py             -- OPTIONAL (--with-search "query"): writes a
#                                       review queue, does NOT auto-approve (see below)
#   7. batch_pipeline.py            -- clone (bounded batches) -> extract -> index,
#                                       --only-unsynced --stats so repos/ never
#                                       accumulates more than one batch's clones
#   8. export_csv.py                -- regenerate skills_export.csv
#
# Every source needs its own refresh -- this is not just a sync-seed thing:
#   - Seed lists (repo-seeds/repo_seeds.json, e.g. officialskills.sh's
#     awesome-list): the VENDORED COPY under repo-seeds/ goes stale the
#     moment upstream adds a repo. refresh_seeds.py (step 1) re-clones the
#     upstream repo and overwrites the vendored copy; sync-seed (step 2)
#     only ever reads what's already vendored -- it can't see past a stale
#     copy. Running sync-seed on its own, repeatedly, without refresh_seeds.py
#     first, silently stops picking up new repos even though it exits 0
#     every time.
#   - Marketplace (fetch_marketplace.py): fetches live from Anthropic's repo
#     every run, so no separate refresh step exists or is needed.
#   - skills.sh leaderboard: the raw snapshot (leaderboard-raw/) is the thing
#     that goes stale here, refreshed by pull_leaderboard.py -- MANUAL-ONLY,
#     see step 4 below. add_skillsh_leaderboard.py (step 5) just reads
#     whatever snapshot already exists, same relationship as sync-seed has
#     to refresh_seeds.py.
#   - GitHub search (search_github.py): each run IS the refresh -- there's no
#     separate vendored copy to go stale, a fresh query against the live API
#     every time you run it with --with-search.
#
# Deliberately NOT included, and never wire in:
#   - pull_leaderboard.py -- MANUAL-ONLY (see README.md / DAILY_JOB.md /
#     pull_leaderboard.py's own docstring, all three say this). Needs a
#     hand-refreshed VERCEL_OIDC_TOKEN that expires and can't be renewed by a
#     script -- there is no way to make this step non-interactive. The call
#     below is commented out ON PURPOSE, not missing by oversight:
#
#       # python3 pull_leaderboard.py 1000
#
#     Run that line yourself, by hand, at the terminal, only when you
#     deliberately want a fresh leaderboard snapshot. --with-leaderboard
#     (step 4) only CONSUMES whatever snapshot already exists in
#     leaderboard-raw/; it never pulls one itself.
#   - GitHub search approval -- search_github.py's results are a review queue
#     a human must read before anything gets cloned (see DAILY_JOB.md's
#     warning about not silently approving unreviewed results). RUN.sh prints
#     the exact `registry.py add-search --approve ...` command to run by hand
#     afterward; it never approves anything on its own.
#
# See also: README.md "Running things -- full reference" for how this fits
# alongside batch_pipeline.py / archived/run_pipeline.sh, and DAILY_JOB.md
# for the recurring maintenance workflow (registry review, blacklisting,
# etc.) this script doesn't attempt to replace.
#
# Usage:
#   ./RUN.sh                                # refresh seeds + sync-seed + marketplace + batched clone/extract/index + csv
#   ./RUN.sh --with-search "agent skills"    # also run a GitHub search (still needs manual approval)
#   ./RUN.sh --with-leaderboard              # also upsert ranks from an existing leaderboard-raw/ snapshot
#   ./RUN.sh --batch-size 50                 # override the default batch size (100)
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "[error] .venv/bin/python not found -- run the Setup steps in README.md first:" >&2
  echo "        python3 -m venv .venv && .venv/bin/python -m pip install \"qdrant-client[fastembed]\"" >&2
  exit 1
fi

# qdrant_db/ is a local embedded store, not a server -- it does NOT support
# concurrent writers (see DAILY_JOB.md's "Non-obvious issues"). Guard against
# two RUN.sh / pipeline invocations overlapping (e.g. a cron job that takes
# longer than its own interval) instead of hitting a confusing mid-run crash.
# `mkdir` is used instead of flock(1) since flock isn't available on macOS by
# default (it's a Linux-only util-linux tool) -- mkdir is atomic on both.
LOCK_DIR=".run.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[error] another RUN.sh (or pipeline step) appears to already be running --" >&2
  echo "        $LOCK_DIR exists. Wait for it to finish, or remove that directory" >&2
  echo "        by hand if you're sure nothing is actually running." >&2
  exit 1
fi
trap 'rmdir "$LOCK_DIR"' EXIT

BATCH_SIZE=100
WITH_SEARCH=""
WITH_LEADERBOARD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-search)
      WITH_SEARCH="$2"; shift 2 ;;
    --with-leaderboard)
      WITH_LEADERBOARD=1; shift ;;
    --batch-size)
      BATCH_SIZE="$2"; shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: ./RUN.sh [--with-search \"query\"] [--with-leaderboard] [--batch-size N]" >&2
      exit 1 ;;
  esac
done

echo "[1/8] refresh_seeds.py (re-vendors seed lists like officialskills.sh from their upstream repos)"
python3 refresh_seeds.py

echo "[2/8] registry.py sync-seed (additive: pick up new repos from the just-refreshed vendored copy)"
python3 registry.py sync-seed

echo "[3/8] fetch_marketplace.py (additive: pick up new repos from the Claude plugin marketplace)"
python3 fetch_marketplace.py

echo "[4/8] pull_leaderboard.py -- NEVER run by this script, see the header comment. Skipping."
# python3 pull_leaderboard.py 1000
#
# ^ Intentionally commented out -- MANUAL-ONLY, needs a hand-refreshed
# VERCEL_OIDC_TOKEN a script can't renew itself. Uncomment and run this line
# yourself at the terminal (not via RUN.sh) if you want a fresh snapshot;
# see README.md "Authentication" for how to refresh VERCEL_OIDC_TOKEN first.

if [[ "$WITH_LEADERBOARD" == 1 ]]; then
  if [[ -f leaderboard-raw/combined.json ]]; then
    echo "[5/8] add_skillsh_leaderboard.py (upserts rank data from the existing leaderboard-raw/ snapshot)"
    python3 add_skillsh_leaderboard.py
  else
    echo "[5/8] --with-leaderboard given but leaderboard-raw/combined.json is missing." >&2
    echo "       Run this by hand first (manual-only, needs a fresh VERCEL_OIDC_TOKEN):" >&2
    echo "         python3 pull_leaderboard.py 1000" >&2
    echo "       Continuing without a leaderboard refresh."
  fi
else
  echo "[5/8] skipping skills.sh leaderboard sync (pass --with-leaderboard to run it," \
       "after pulling a fresh snapshot yourself with pull_leaderboard.py)"
fi

if [[ -n "$WITH_SEARCH" ]]; then
  echo "[6/8] search_github.py \"$WITH_SEARCH\" (writes a review queue -- NOT auto-approved)"
  python3 search_github.py "$WITH_SEARCH" --exact --format json --top 25 \
      --out repo-seeds/github_search_results.json
  echo "      -> review repo-seeds/github_search_results.json yourself, then approve what you want:"
  echo "         ./registry.py add-search repo-seeds/github_search_results.json --approve owner/repo --approve owner2/repo2 ..."
else
  echo "[6/8] skipping GitHub search (pass --with-search \"query\" to run one)"
fi

echo "[7/8] batch_pipeline.py --batch-size $BATCH_SIZE --only-unsynced --stats (clone -> extract -> index, in batches)"
.venv/bin/python batch_pipeline.py --batch-size "$BATCH_SIZE" --only-unsynced --stats

echo "[8/8] export_csv.py (regenerate skills_export.csv)"
.venv/bin/python export_csv.py

echo
echo "[done] RUN.sh finished at $(date)"
echo "Run ./stats.py any time for a status snapshot; stats.log has the batch-by-batch trend from this run."
