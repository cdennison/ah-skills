"""CLI / dependency security-scan pipeline for the skills corpus.

Grep search-raw/ for install commands -> classify npm/pip packages as CLI vs
library -> audit against OSV.dev -> map advisories back to skills -> write a
`cli_security` verdict onto each skill's Qdrant point.

Design: ../docs/ARCHITECTURE_CLI_SECURITY_SCAN.md
Run:    ./run.sh  (then build_cli_export.py)
"""
