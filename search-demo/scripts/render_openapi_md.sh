#!/usr/bin/env bash
# Moved to vettd-e2e/scripts/render-query-service-api.sh (2026-09-05) — the
# rendering tooling for cross-system specs is centralized there, alongside
# the doc it produces (docs/specs/query-service-api.md). See
# vettd-e2e/docs/conventions/doc-placement.md.
#
# This forwards to that script so the old invocation still works from a
# sibling checkout.
set -euo pipefail
TARGET="$(dirname "$0")/../../vettd-e2e/scripts/render-query-service-api.sh"
if [ ! -x "$TARGET" ]; then
  echo "vettd-e2e sibling checkout not found — clone it alongside ah-skills" \
       "(see vettd-e2e/docs/system/repos.md), or run" \
       "vettd-e2e/scripts/render-query-service-api.sh directly." >&2
  exit 1
fi
exec "$TARGET" "$@"
