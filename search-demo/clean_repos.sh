#!/usr/bin/env bash
# Delete the transient repos/ clone directory. Pinned to this exact path so a
# bad call site (wrong cwd, empty var, etc.) can never turn into an rm -rf of
# something else.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOS_DIR="${SCRIPT_DIR}/repos"

# Guard: must be an absolute path, must be exactly <script_dir>/repos, must
# not be "/" or empty.
if [[ -z "$REPOS_DIR" || "$REPOS_DIR" == "/" || "$REPOS_DIR" != "${SCRIPT_DIR}/repos" ]]; then
    echo "[clean_repos] refusing to delete, unexpected path: ${REPOS_DIR}" >&2
    exit 1
fi

if [[ ! -d "$REPOS_DIR" ]]; then
    echo "[clean_repos] ${REPOS_DIR} does not exist, nothing to do"
    exit 0
fi

echo "[clean_repos] removing ${REPOS_DIR}"
rm -rf -- "${REPOS_DIR:?}"
