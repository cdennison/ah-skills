#!/usr/bin/env bash
# E2E: confirm discover_assets.py against a real plugin install, in a throwaway
# Docker sandbox. Nothing touches the host's ~/.claude.
#
#   e2e/run_e2e.sh <repo-path> [<repo-path> ...]
#
# For each repo:
#   1. docker: install every plugin the repo's marketplace(s) declare, record
#      the exact installed file tree + `claude plugin details`
#   2. host:   run discover_assets.py on the repo
#   3. host:   compare_install.py — every installed file must be a catalogued
#      asset or an explicit exclusion
#
# Cleanup is automatic: `docker run --rm` removes the container; the sandbox
# Claude config only ever exists inside it. Pass --rmi to also delete the image.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
POC="$(dirname "$HERE")"
IMAGE="plugin-scan-e2e"
OUTROOT="$HERE/out"
RMI=0
REPOS=()
for a in "$@"; do
  [ "$a" = "--rmi" ] && { RMI=1; continue; }
  REPOS+=("$a")
done
[ "${#REPOS[@]}" -gt 0 ] || { echo "usage: $0 <repo> [<repo> ...] [--rmi]"; exit 2; }

echo ">> building sandbox image ($IMAGE)…"
docker build -q -t "$IMAGE" "$HERE" >/dev/null

rc=0
for repo in "${REPOS[@]}"; do
  repo="$(cd "$repo" && pwd)"
  name="$(basename "$repo")"
  out="$OUTROOT/$name"
  rm -rf "$out"; mkdir -p "$out"

  echo
  echo "============================================================"
  echo ">> $name"
  echo "============================================================"

  echo ">> [1/3] docker: installing plugins in the sandbox…"
  docker run --rm \
    -v "$repo":/work/repo:ro \
    -v "$out":/out \
    "$IMAGE" /work/repo /out

  echo ">> [2/3] host: discover_assets.py…"
  python3 "$POC/discover_assets.py" "$repo" -o "$out/$name.assets.json"

  echo ">> [3/3] host: compare installed tree vs catalogue…"
  shopt -s nullglob
  for inst in "$out"/*.installed.txt; do
    det="${inst%.installed.txt}.details.txt"
    echo
    echo "--- $(basename "${inst%.installed.txt}") ---"
    python3 "$HERE/compare_install.py" "$out/$name.assets.json" "$inst" \
      ${det:+--details "$det"} || rc=1
  done
done

if [ "$RMI" = 1 ]; then
  echo ">> removing image $IMAGE"
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
fi

echo
echo ">> e2e output under $OUTROOT/"
exit $rc
