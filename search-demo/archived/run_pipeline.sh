#!/usr/bin/env bash
# Run the full clone -> extract -> index pipeline. Safe to run multiple
# times a day: clone_repos.py has its own per-repo 24h skip (.clone_state.json),
# so re-running costs nothing beyond a directory-existence check for repos
# it already has; extract_search_raw.py is a full rescan of repos/ (seconds,
# not rate-limited); index_qdrant.py is incremental (hash-diff, only embeds
# new/changed files), so a same-day re-run finishes almost instantly if
# nothing changed.
#
# ⚠️  For a LARGE run (most of the registry not yet synced, or a big batch
# of newly-added repos), use batch_pipeline.py instead -- this script's
# clone_repos.py step clones everything into repos/ (full git clones) in
# one shot before extract_search_raw.py ever runs, which can fill the disk
# for a big enough registry. batch_pipeline.py clones bounded batches and
# deletes repos/ between them:
#   python3 batch_pipeline.py --batch-size 100 --only-unsynced --stats
#
# Usage:
#   ./run_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")"

source .venv/bin/activate

echo "[1/5] fetch_marketplace.py (additive: adds new repos from the Claude plugin marketplace)"
python3 fetch_marketplace.py

echo "[2/5] clone_repos.py (rate-limited internally; skips repos cloned <24h ago)"
python3 clone_repos.py

echo "[3/5] extract_search_raw.py"
python3 extract_search_raw.py

echo "[4/5] index_qdrant.py (incremental)"
python3 index_qdrant.py

echo "[5/5] registry.py mark-synced (stamp last_synced on every repo now on disk)"
python3 registry.py mark-synced

echo "[done] pipeline finished at $(date)"
