#!/usr/bin/env bash
# Runs INSIDE the sandbox container. Installs every plugin declared by every
# marketplace.json in the mounted repo, then records exactly what landed on disk
# and what Claude Code recognises as components.
#
#   verify.sh /repo /out
#
# Writes to /out:
#   marketplaces.txt              — `claude plugin marketplace list`
#   installed_plugins.json        — Claude's own record
#   <mkt>__<plugin>.installed.txt — every file in the plugin's install cache dir
#   <mkt>__<plugin>.details.txt   — `claude plugin details` component inventory
#   cache.tree.txt                — full tree of the plugin cache
#   SUMMARY.txt
set -uo pipefail

REPO="${1:?usage: verify.sh <repo> <out>}"
OUT="${2:?usage: verify.sh <repo> <out>}"
mkdir -p "$OUT"
: > "$OUT/SUMMARY.txt"
log() { echo "$*" | tee -a "$OUT/SUMMARY.txt"; }

log "# plugin-install e2e — $(date -u +%FT%TZ)"
log "claude: $(claude --version 2>/dev/null)"
log "repo:   $REPO"
log ""

# A repo may be a marketplace at the root and/or in subdirs.
mapfile -t MKTS < <(find "$REPO" -type f -path '*/.claude-plugin/marketplace.json' \
                    -not -path '*/node_modules/*' -not -path '*/tests/*' -not -path '*/fixtures/*' | sort)

if [ "${#MKTS[@]}" -eq 0 ]; then
  log "no .claude-plugin/marketplace.json found — trying repo root as a plugin dir"
  MKTS=("")
fi

for mkt in "${MKTS[@]}"; do
  if [ -n "$mkt" ]; then
    mdir="$(dirname "$(dirname "$mkt")")"
    log "## marketplace: $mkt"
    claude plugin marketplace add "$mdir" 2>&1 | tee -a "$OUT/SUMMARY.txt"
    mname="$(jq -r '.name' "$mkt")"
    mapfile -t PLUGINS < <(jq -r '.plugins[].name' "$mkt")
  else
    mname=""
    PLUGINS=()
  fi

  for p in "${PLUGINS[@]}"; do
    id="$p@$mname"
    log ""
    log "### install $id"
    claude plugin install "$id" 2>&1 | tee -a "$OUT/SUMMARY.txt"

    # locate the install cache dir for this plugin
    cdir="$(find "$CLAUDE_CONFIG_DIR/plugins/cache/$mname/$p" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)"
    safe="${mname}__${p}"
    if [ -n "$cdir" ] && [ -d "$cdir" ]; then
      ( cd "$cdir" && find . -type f | sed 's|^\./||' | sort ) > "$OUT/$safe.installed.txt"
      n=$(wc -l < "$OUT/$safe.installed.txt")
      log "installed files: $n   (cache: ${cdir#$CLAUDE_CONFIG_DIR/})"
    else
      log "WARNING: could not find install cache dir for $id"
      : > "$OUT/$safe.installed.txt"
    fi
    claude plugin details "$id" > "$OUT/$safe.details.txt" 2>&1 || true
  done
done

claude plugin marketplace list      > "$OUT/marketplaces.txt" 2>&1 || true
claude plugin list                  >> "$OUT/marketplaces.txt" 2>&1 || true
cp "$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json" "$OUT/installed_plugins.json" 2>/dev/null || true
( cd "$CLAUDE_CONFIG_DIR/plugins/cache" 2>/dev/null && find . -type f | sort ) > "$OUT/cache.tree.txt" 2>/dev/null || true

log ""
log "done."
