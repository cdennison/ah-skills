# cli-security-scan

Scan the skills corpus for the third-party **command-line tools** skills tell
you (or the agent) to install, and flag the ones with known security
advisories.

Full design + rationale: [`../docs/ARCHITECTURE_CLI_SECURITY_SCAN.md`](../../../vettd-e2e/docs/specs/architecture-cli-security-scan.md).

## What it produces

A `cli_security` field on each affected skill's Qdrant point:

```json
{
  "grade": "C",
  "scanned_at": "2026-08-30T12:00:00+00:00",
  "osv_snapshot_date": "2026-08-30",
  "packages": [
    {"package": "playwright", "ecosystem": "npm", "classification": "cli",
     "install_command": "npm install -g playwright && npx playwright install chromium",
     "vuln_count": 2, "max_severity": "HIGH",
     "advisory_ids": ["GHSA-...", "GHSA-..."]}
  ]
}
```

`grade` is the worst across `packages[]`: **A** none known, **B** worst is
LOW/MODERATE, **C** worst is HIGH/CRITICAL *or* an advisory OSV gave no
severity label. `export_csv.py` flattens this into the `cli`,
`cli_security_grade`, `cli_security_scan` columns of `skills_export.csv`;
`/query` returns it as `hit.cli_security`.

> **The grade means "a tool this skill installs has a security history", not
> "you are vulnerable".** OSV is queried without a version (install commands
> rarely pin one), so every historical advisory counts. Resolve
> `advisory_ids` for the actual affected ranges.

## Run it

Needs `search-raw/` populated and (for the payload write) `agent_skills`
indexed in Qdrant.

```bash
cd search-demo/cli-security-scan

./run.sh                       # grep -> extract -> classify -> OSV audit -> map
                               #   writes work/ (gitignored), caches every HTTP response
uv run python build_cli_export.py            # write cli_security onto agent_skills
uv run python build_cli_export.py --dry-run  # preview, write nothing
uv run python build_cli_export.py --csv      # offline: work/skills_export_cli.csv, no Qdrant
```

Or via the top-level pipeline: `./RUN.sh --with-cli-scan` runs `run.sh` +
`build_cli_export.py` as step 8.5, right after indexing.

`build_cli_export.py` reads Qdrant selection from `SKILLS_QDRANT_URL` or
`SKILLS_QDRANT_DB_PATH` (one, not both), same as `publish_scans.py`.

## Refresh

Advisory data drifts. Package classification does not (a package doesn't
change ecosystem), so keep that cache:

```bash
rm -rf work/cache/osv/
./run.sh
uv run python build_cli_export.py --force    # --force: package set unchanged but severities moved
```

A full rebuild from nothing:

```bash
rm -rf work/                                 # drops the mentions log + every cache
./run.sh --refresh
```

`./run.sh` reuses an existing `work/install_mentions.log` (the slow step —
a full sweep of `search-raw/`) unless `--refresh` is given or it's missing.

Monthly is a reasonable cadence — the npm/PyPI advisory databases move slowly
for the long tail of CLI tools.

## Files

| File | Role |
|---|---|
| `find_install_mentions.py` | regex sweep of `search-raw/` → `work/install_mentions.log` (paths relative to `search-raw/`) |
| `extract_packages.py` | `{npm\|pip} {extract\|classify}` — parse trusted install commands, then classify each package CLI vs library via registry metadata |
| `audit_packages.py` | `{npm\|pip}` — OSV.dev query per CLI/likely-CLI package |
| `map_to_skills.py` | `{npm\|pip}` — advisory → skills that install the package |
| `build_cli_export.py` | the `cli_security` verdict: Qdrant `set_payload` (default) or `--csv` |
| `skill_id_util.py` | file path → enclosing skill directory (filesystem-verified) |
| `_common.py` | shared paths + cached, backoff-retrying JSON fetch |
| `run.sh` | chains the six report steps |
| `work/` | all intermediates + `work/cache/` — gitignored, regenerable |

## Smoke test

[`../smoke_cli_security_context7.py`](../smoke_cli_security_context7.py) —
runs the pipeline end to end (live npm + OSV) for the context7 CLI
(`npx -y @upstash/context7-mcp`), the exact `claude mcp add … -- npx` shape a
real indexed skill uses.
