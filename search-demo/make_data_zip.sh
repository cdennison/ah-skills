#!/usr/bin/env bash
# Zips repo-seeds/ (git-tracked, included redundantly so the zip is a
# self-contained snapshot even without git) plus the generated, gitignored
# data (repos/, search-raw/, qdrant_db/) into search_demo_data.zip, for
# upload as a GitHub Release asset and restore on another machine without
# re-running clone_repos.py / index_qdrant.py.
set -euo pipefail

cd "$(dirname "$0")"

OUT="search_demo_data.zip"

rm -f "$OUT"
zip -r "$OUT" repo-seeds repos search-raw qdrant_db -x '.DS_Store'

echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
