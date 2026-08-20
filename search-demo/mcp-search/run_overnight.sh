#!/usr/bin/env bash
# Overnight MCP pipeline run: pull up to 100K entries from each of the
# official MCP registry and Glama (both will simply exhaust their own
# pagination cursor and stop early if the real total is under 100K -- see
# pull_official_registry.py/pull_glama.py's fetch_all()), then download
# readmes for every newly-discovered unique repo, then reclassify, then
# backfill GitHub stars/npm ranking data and OSV.dev security-scan data
# (see fetch_mcp_rankings.py/fetch_mcp_security.py -- both used to be
# separate, manually-invoked scripts; wiring them in here is what makes
# them a real part of "the pipeline" instead of something that only runs
# when someone remembers to kick it off by hand), then export and snapshot
# stats. Expected to take a long time, potentially 20+ hours -- the
# rankings step alone is that order of magnitude (see its own docstring) --
# that's fine, it's meant to run unattended overnight.
#
# Rate limits are enforced inside each script (shared/http.py: 10/s+100/min+
# 10000/hr for the two registry APIs, 4000/hr shared across every GitHub
# host for readme downloads, 70-minute sleep-and-retry on any 429) -- this
# wrapper adds no pacing of its own, only sequencing and crash resilience.
#
# rankings and security run through supervise.sh (their own retry-on-crash
# wrapper, invoked here WITHOUT & so this script blocks on each in turn)
# rather than a bare `python3 -u ...` -- both scripts' own docstrings say
# "meant to be run under supervise.sh." Deliberately sequenced, never
# concurrent with each other or with the pull/readme/classify steps above:
# every one of these steps reads the whole registry into memory and writes
# the whole file back out, so two of them running at once silently clobber
# whichever one saves last -- confirmed painfully in practice (a supervised
# rankings job got SIGTERM'd mid-write once and left registry.json
# completely empty; see mcp_registry.save_registry()'s docstring for the
# atomic-write fix that followed, and rebuild_registry_from_raw.py for the
# recovery path that incident needed). Sequencing here is what keeps a
# routine overnight run from ever needing that recovery path.
#
# NOT `set -e`: a non-zero exit from download_readmes.py (e.g. an
# unexpected network exception mid-run, hours in) should be retried, not
# abort the whole night's progress -- download_readmes.py is idempotent on
# resume (it skips any repo that already has a readme_path), so restarting
# it after a crash just continues from where it left off.
set -uo pipefail

cd "$(dirname "$0")"
export PATH="$(cd .. && pwd)/.venv/bin:$PATH"

echo "=== $(date) overnight run start ==="

echo "--- official registry (up to 100K) ---"
python3 -u pull_official_registry.py --limit 100000

echo "--- glama (up to 100K) ---"
python3 -u pull_glama.py --limit 100000

echo "--- readme download (resilience loop) ---"
# --no-clone: mid-run data review found tier 3 (shallow clone) recovered
# exactly 1 readme out of 1,161 attempts (0.09%) -- essentially all of
# those 1,161 were genuinely dead/private repos that tier 1+2 already
# correctly identified as gone, so tier 3 was just paying subprocess+git-
# handshake overhead for almost no return. Worse, each clone attempt only
# counts as ONE unit against the 4000/hr budget (one limiter.wait() call)
# even though a real clone handshake can be 2 actual HTTP requests to
# github.com -- at scale that under-counts real GitHub load against the
# budget we're trying to stay safely under. Disabling it for this bulk run
# trades a ~0.1% readme-recovery rate for both more accurate rate
# accounting and meaningfully less wall-clock overhead -- worth it given
# the 100K-in-20h target. The tier still exists in download_readmes.py for
# smaller/targeted runs where that trade isn't worth making.
MAX_ATTEMPTS=20
attempt=1
until python3 -u download_readmes.py --no-clone; do
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
        echo "[giving up] download_readmes.py failed $attempt times in a row -- stopping the retry loop"
        break
    fi
    echo "[retry $attempt/$MAX_ATTEMPTS] download_readmes.py exited non-zero -- sleeping 60s and resuming"
    attempt=$((attempt + 1))
    sleep 60
done

echo "--- reclassify ---"
python3 -u classify_mcp_registry.py

echo "--- rankings (github stars + npm downloads/score, ~20h) ---"
./supervise.sh start rankings python3 -u fetch_mcp_rankings.py

echo "--- security scan (osv.dev, direct-dependency pass included) ---"
./supervise.sh start security python3 -u fetch_mcp_security.py

echo "--- csv export ---"
python3 -u export_mcp_csv.py

echo "--- final stats ---"
python3 -u mcp_stats.py

echo "=== $(date) overnight run complete ==="
