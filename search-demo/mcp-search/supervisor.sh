#!/usr/bin/env bash
# Poor-man's supervisor for run_overnight.sh: if the whole pipeline process
# dies outright (crash, OOM-kill, a signal, an unhandled exception in a
# step that isn't wrapped in its own retry loop) rather than exiting
# cleanly, relaunch it from the top. No systemd/cron dependency, no sudo,
# no packages installed, no state left behind on the box once the run is
# done -- just a bash retry loop, which is all a one-night batch job needs:
#
#   1. A step inside run_overnight.sh crashes -> that script's own inner
#      retry loop (around download_readmes.py specifically) handles it
#      without restarting the whole pipeline; for anything else, this
#      outer loop relaunches run_overnight.sh from the top. That's cheap,
#      not wasteful: pull_official_registry.py/pull_glama.py's output is a
#      full re-pull each time (idempotent -- re-upserting the same servers
#      just refreshes them), and download_readmes.py/classify_mcp_registry.py
#      both skip/redo cheaply on top of whatever's already in registry.json,
#      so re-running earlier steps after a crash further in doesn't lose or
#      corrupt anything.
#   2. The whole process gets killed outright (OOM, signal) -- caught the
#      same way, since a killed process is still a non-zero/abnormal exit
#      from this loop's point of view.
#
# NOT covered: the host rebooting, or this loop's own parent shell being
# killed -- surviving that needs a reboot-persistent mechanism (cron
# @reboot or a systemd unit), which was considered and skipped here: cron
# isn't installed on this box, and adding one (or a systemd unit) means a
# package install + a persistent entry outliving this one-off job. If that
# stronger guarantee ever matters: `sudo dnf install cronie` + a `crontab -e`
# entry checking a pidfile every few minutes is the natural next step.
set -u

cd "$(dirname "$0")"
export PATH="$(cd .. && pwd)/.venv/bin:$PATH"

MAX_ATTEMPTS=10
BACKOFF_SECONDS=30
LOG_DIR=".overnight_logs"
mkdir -p "$LOG_DIR"

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    run_log="$LOG_DIR/attempt_${attempt}_$(date +%Y%m%d_%H%M%S).log"
    echo "$(date): [supervisor] starting run_overnight.sh -- attempt $attempt/$MAX_ATTEMPTS (log: $run_log)"

    ./run_overnight.sh > "$run_log" 2>&1
    status=$?

    if [ "$status" -eq 0 ]; then
        echo "$(date): [supervisor] run_overnight.sh finished successfully on attempt $attempt"
        exit 0
    fi

    echo "$(date): [supervisor] run_overnight.sh exited with status $status (attempt $attempt/$MAX_ATTEMPTS) -- sleeping ${BACKOFF_SECONDS}s and retrying"
    attempt=$((attempt + 1))
    sleep "$BACKOFF_SECONDS"
done

echo "$(date): [supervisor] giving up after $MAX_ATTEMPTS attempts -- see $LOG_DIR/ for each attempt's log"
exit 1
