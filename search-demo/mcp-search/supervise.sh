#!/usr/bin/env bash
# Generic poor-man's supervisor for any long-running script in this
# directory -- same retry-loop shape as supervisor.sh (which wraps
# run_overnight.sh specifically), generalized so a future long job (like
# fetch_mcp_rankings.py's ~20h GitHub-star pass) doesn't need its own
# bespoke wrapper. Relaunches the given command from the top if it dies
# (crash, OOM-kill, unhandled exception, anything that isn't a clean exit
# 0), logs each attempt separately, and tracks a pidfile so a later session
# can tell whether it's still running instead of double-launching a second
# copy against the same registry.json.
#
# Usage:
#   ./supervise.sh start  <name> <command> [args...]
#   ./supervise.sh status <name>
#   ./supervise.sh stop   <name>
#
#   ./supervise.sh start rankings python3 -u fetch_mcp_rankings.py
#   ./supervise.sh start rankings-stars-only python3 -u fetch_mcp_rankings.py --stars-only
#   ./supervise.sh status rankings
#   ./supervise.sh stop rankings
#
# <name> is just a label -- becomes the log dir (.supervisor_logs/<name>/)
# and pidfile name, nothing else. Pick a fresh name to run the same script
# under two supervised instances at once (e.g. a --stars-only run and a
# --downloads-only run in parallel); reuse a name and `start` will refuse
# to launch a second copy while the first is still alive.
#
# To rerun a job that already finished (or was stopped): just `start` it
# again with the same command -- every long job in this pipeline is
# designed to be resumable (mcp_registry.py rows track their own freshness,
# e.g. fetch_mcp_rankings.py's --stale-days skip), so relaunching after a
# clean or unclean stop naturally picks up wherever registry.json left off,
# not from zero.
#
# NOT covered (same limitation as supervisor.sh, documented there in more
# detail): a host reboot, or this loop's own parent shell being killed
# outright (e.g. `kill -9` on the supervisor itself, which never reaches the
# EXIT trap). Surviving that needs cron @reboot or a systemd unit, not
# installed on this box -- if that stronger guarantee ever matters:
# `sudo dnf install cronie` + a crontab entry checking each pidfile.
set -u

cd "$(dirname "$0")"
export PATH="$(cd .. && pwd)/.venv/bin:$PATH"

MAX_ATTEMPTS="${SUPERVISE_MAX_ATTEMPTS:-10}"
BACKOFF_SECONDS="${SUPERVISE_BACKOFF_SECONDS:-30}"

usage() {
    echo "usage: $0 start <name> <command> [args...]" >&2
    echo "       $0 status <name>" >&2
    echo "       $0 stop <name>" >&2
    exit 2
}

log_dir_for() { echo ".supervisor_logs/$1"; }

cmd_start() {
    local name="$1"; shift
    [ "$#" -ge 1 ] || usage
    local log_dir; log_dir="$(log_dir_for "$name")"
    local pid_file="$log_dir/supervisor.pid"
    mkdir -p "$log_dir"

    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "[supervisor:$name] already running under pid $(cat "$pid_file") -- refusing to start a second copy" >&2
        echo "  check progress: $0 status $name" >&2
        exit 1
    fi
    echo $$ > "$pid_file"

    # Two separate traps, deliberately: EXIT alone (fires on every exit path,
    # including a normal `exit 0`/`exit 1` below) just cleans up. INT/TERM
    # must ALSO call `exit` explicitly -- bash does not terminate a script
    # after running a trapped signal's handler by default, it resumes
    # wherever execution was interrupted (e.g. mid `sleep $BACKOFF_SECONDS`),
    # which is exactly the bug a first version of this script had: `stop`
    # sent SIGTERM, the handler ran, and the retry loop just continued
    # right past it. `exit 143` (128+SIGTERM) below both terminates for real
    # and triggers the EXIT trap in turn, so cleanup still runs exactly once.
    child_pid=""
    cleanup() {
        rm -f "$pid_file"
        if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
            kill -TERM "$child_pid" 2>/dev/null
        fi
    }
    trap cleanup EXIT
    trap 'echo "$(date): [supervisor:$name] caught signal -- stopping"; exit 143' INT TERM

    attempt=1
    while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
        run_log="$log_dir/attempt_${attempt}_$(date +%Y%m%d_%H%M%S).log"
        echo "$(date): [supervisor:$name] starting '$*' -- attempt $attempt/$MAX_ATTEMPTS (log: $run_log)"

        "$@" > "$run_log" 2>&1 &
        child_pid=$!
        echo "$child_pid" > "$log_dir/current_child.pid"
        wait "$child_pid"
        status=$?
        child_pid=""

        if [ "$status" -eq 0 ]; then
            echo "$(date): [supervisor:$name] finished successfully on attempt $attempt"
            rm -f "$log_dir/current_child.pid"
            exit 0
        fi

        echo "$(date): [supervisor:$name] exited with status $status (attempt $attempt/$MAX_ATTEMPTS) -- sleeping ${BACKOFF_SECONDS}s and retrying"
        attempt=$((attempt + 1))
        sleep "$BACKOFF_SECONDS"
    done

    echo "$(date): [supervisor:$name] giving up after $MAX_ATTEMPTS attempts -- see $log_dir/ for each attempt's log"
    rm -f "$log_dir/current_child.pid"
    exit 1
}

cmd_status() {
    local name="$1"
    local log_dir; log_dir="$(log_dir_for "$name")"
    local pid_file="$log_dir/supervisor.pid"

    if [ ! -d "$log_dir" ]; then
        echo "[$name] no such supervised job (no $log_dir/)"
        return 1
    fi

    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "[$name] RUNNING -- supervisor pid $(cat "$pid_file")"
        if [ -f "$log_dir/current_child.pid" ]; then
            echo "  current child pid: $(cat "$log_dir/current_child.pid")"
        fi
    else
        echo "[$name] NOT running (stale or no pidfile)"
    fi

    local latest; latest="$(ls -t "$log_dir"/attempt_*.log 2>/dev/null | head -1)"
    if [ -n "$latest" ]; then
        echo "  latest log: $latest"
        echo "  --- last 10 lines ---"
        tail -10 "$latest" | sed 's/^/  /'
    fi
}

cmd_stop() {
    local name="$1"
    local log_dir; log_dir="$(log_dir_for "$name")"
    local pid_file="$log_dir/supervisor.pid"

    if [ ! -f "$pid_file" ] || ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "[$name] not running"
        return 1
    fi
    local pid; pid="$(cat "$pid_file")"
    echo "[$name] stopping supervisor pid $pid (and its current child, via trap)"
    kill -TERM "$pid"
}

[ "$#" -ge 1 ] || usage
action="$1"; shift
case "$action" in
    start)  [ "$#" -ge 2 ] || usage; cmd_start "$@" ;;
    status) [ "$#" -eq 1 ] || usage; cmd_status "$@" ;;
    stop)   [ "$#" -eq 1 ] || usage; cmd_stop "$@" ;;
    *) usage ;;
esac
