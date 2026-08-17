#!/usr/bin/env bash
# Hourly Claude Code health check on the overnight MCP pipeline, for up to
# HOURS iterations. Not a cron job (none installed on this box) and not a
# cloud routine (the /schedule cloud agents run in an isolated sandbox with
# no access to this machine's processes/files, which is exactly what this
# check needs to inspect) -- just a local loop invoking `claude -p`
# headlessly once an hour. Each invocation is a genuinely fresh Claude
# session with zero memory of anything before it, so the prompt below is
# fully self-contained and only ever points at stable, project-local paths
# (registry.json, .overnight_logs/*.log) -- never a /tmp scratchpad path,
# which is scoped to one specific interactive session and isn't a
# reasonable thing for an unrelated headless session to depend on.
set -u
cd "$(dirname "$0")"

HOURS=10
INTERVAL_SECONDS=3600
LOG_FILE="claude_supervisor.log"

read -r -d '' PROMPT_TEMPLATE <<'EOF' || true
You are doing periodic check __CHECK_NUM__/__TOTAL_CHECKS__ (hourly) on an
overnight batch pipeline. You have no memory of any prior check -- this is
a fresh session. Use ONLY the Bash tool. Working directory:
/home/ec2-user/ah-skills/search-demo/mcp-search/

Background: this pipeline discovers MCP (Model Context Protocol) servers
from the official MCP registry, Glama's directory, and a seed awesome-list,
then downloads a README for each and classifies it. It's launched via
./supervisor.sh (a self-relaunching wrapper, up to 10 attempts) ->
./run_overnight.sh (pulls official registry -> pulls glama -> downloads
readmes with --no-clone, itself with its own internal retry loop ->
classifies -> exports CSV -> generates stats). It's expected to run for
many hours -- long runtime alone is not a problem to report.

Do exactly this, in order:

1. Check whether it's currently running:
   ps aux | grep -E "supervisor\.sh|run_overnight\.sh|pull_official_registry|pull_glama|download_readmes|classify_mcp_registry|export_mcp_csv|mcp_stats" | grep -v grep

2. Find the current stage: list .overnight_logs/ (there's one attempt_N_*.log
   per supervisor.sh restart of run_overnight.sh), identify the most recent
   one by filename timestamp, and tail its last ~60 lines.

3. In that same tail, look for problems: rate-limit sleep messages
   (grep for "[rate-limit]", the literal bracketed tag the pipeline prints
   -- NOT a bare "429" or "403" substring search, which false-positives
   heavily on progress counters like "[40312/67184]", byte counts like
   "8403 chars", or numeric repo IDs that happen to contain those digits;
   confirmed this exact false-positive pattern in a real check), Python
   tracebacks, repeated identical failures. A "[rate-limit]" sleep message
   is the system working as designed, not a problem -- only flag it if it
   recurs many times in a row without progress resuming after. For a more
   reliable check of real HTTP errors specifically, prefer querying
   registry.json's structured `errors` field over grepping raw log text --
   e.g.:
   ../.venv/bin/python3 -c "import json; d=json.load(open('mcp-repo-seeds/registry.json')); print(sum(1 for r in d for e in r.get('errors',[]) if '403' in (e.get('message') or '')))"

4. Sanity-check the registry: confirm mcp-repo-seeds/registry.json is valid
   JSON and get its row count. Use the project's .venv if present:
   ls ../.venv/bin/python3 2>/dev/null && ../.venv/bin/python3 -c "import json; print(len(json.load(open('mcp-repo-seeds/registry.json'))))" || python3 -c "import json; print(len(json.load(open('mcp-repo-seeds/registry.json'))))"

5. Count the attempt_*.log files in .overnight_logs/ -- this is how many
   times supervisor.sh has (re)started run_overnight.sh. A count of 1-3 by
   now is normal churn, not a problem. Only flag it as a real concern if it
   looks like it's climbing every single check with no forward progress in
   the logs between restarts.

6. ONLY if the pipeline is not currently running (nothing matched in step 1)
   AND it has not already finished successfully (the most recent attempt
   log does NOT end with "overnight run complete", or
   mcp_servers_export.csv / a final stats summary look incomplete/missing) --
   relaunch it yourself, fully detached so it survives this session exiting:
   cd /home/ec2-user/ah-skills/search-demo/mcp-search && nohup ./supervisor.sh > .overnight_logs/supervisor_relaunch_$(date +%s).log 2>&1 < /dev/null & disown
   Then note in your log entry that you relaunched it and why. Do NOT
   relaunch it if it's already running, or if it already finished
   successfully.

7. Append exactly ONE new entry to
   /home/ec2-user/ah-skills/search-demo/mcp-search/claude_supervisor.log
   (create the file if it doesn't exist yet) via Bash, in this exact format:

=== $(date -u +%Y-%m-%dT%H:%M:%SZ) check __CHECK_NUM__/__TOTAL_CHECKS__ ===
Status: <running | not running | finished successfully | finished with errors>
Stage: <brief, e.g. "readme download, ~41000/58000 candidates processed">
Registry: <row count, or "unreadable: <error>">
Restarts so far: <attempt_*.log count>
Issues found: <none, or a concise 1-2 sentence description>
Action taken: <none, or "relaunched supervisor.sh -- was not running and had not finished">

Keep the entry under 10 lines. This is a read-only health check except for
the log append and the conditional relaunch in step 6 -- do not modify,
delete, or "fix" anything else, and do not re-run any pipeline step
yourself individually (only supervisor.sh, and only per step 6's condition).
EOF

for i in $(seq 1 "$HOURS"); do
    prompt="${PROMPT_TEMPLATE//__CHECK_NUM__/$i}"
    prompt="${prompt//__TOTAL_CHECKS__/$HOURS}"

    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ): [hourly-check] running check $i/$HOURS" >> "$LOG_FILE.driver"
    claude -p "$prompt" --allowedTools "Bash" --permission-mode bypassPermissions --model claude-sonnet-5 \
        >> "$LOG_FILE.driver" 2>&1

    if [ "$i" -lt "$HOURS" ]; then
        sleep "$INTERVAL_SECONDS"
    fi
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ): [hourly-check] all $HOURS checks complete" >> "$LOG_FILE.driver"
