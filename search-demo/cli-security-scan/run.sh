#!/usr/bin/env bash
# Build the CLI-security reports end to end:
#   grep search-raw/ -> extract npm/pip packages -> classify (CLI vs library)
#   -> audit against OSV.dev -> map advisories back to skills.
#
# All output lands in work/ (gitignored). Registry + OSV responses are cached
# under work/cache/, so a second run is network-free. To refresh advisory
# data, delete work/cache/osv/ (see README.md) and re-run.
#
# After this, write the verdict onto the index:
#   uv run python build_cli_export.py            # -> cli_security payload field
#   uv run python build_cli_export.py --csv      # -> work/skills_export_cli.csv (offline)
#
# Usage: ./run.sh [--refresh]     (--refresh ignores all caches)
set -euo pipefail
cd "$(dirname "$0")"

REFRESH=""
[[ "${1:-}" == "--refresh" ]] && REFRESH="--refresh"

PY=(uv run python)
command -v uv >/dev/null 2>&1 || PY=(python3)

if [[ -z "$REFRESH" && -s work/install_mentions.log ]]; then
  echo "[1/6] find_install_mentions.py -- reusing existing work/install_mentions.log ($(wc -l < work/install_mentions.log) lines; pass --refresh to rescan)"
else
  echo "[1/6] find_install_mentions.py"
  "${PY[@]}" find_install_mentions.py
fi

for eco in npm pip; do
  echo "[2/6] extract_packages.py $eco extract"
  "${PY[@]}" extract_packages.py "$eco" extract
  echo "[3/6] extract_packages.py $eco classify $REFRESH"
  "${PY[@]}" extract_packages.py "$eco" classify $REFRESH
  echo "[4/6] audit_packages.py $eco $REFRESH"
  "${PY[@]}" audit_packages.py "$eco" $REFRESH
  echo "[5/6] map_to_skills.py $eco"
  "${PY[@]}" map_to_skills.py "$eco"
done

echo "[6/6] reports ready in work/:"
ls -1 work/*_security_report_with_skills.csv
echo
echo "Next: uv run python build_cli_export.py   (add --dry-run to preview)"
