# CLI / dependency security-scan step for the skills corpus

> **Status: incorporated.** The `temp/` prototype (commit 3ef5daf) is now
> `cli-security-scan/` — merged npm/pip scripts, an HTTP response cache,
> `search-raw`-relative paths, NUL-safe CSV reads, hermetic tests. The verdict
> is written onto the Qdrant point payload as a `cli_security` field
> (`build_cli_export.py`), preserved across re-index
> (`index_qdrant._preserve_scan_publications`), flattened into
> `skills_export.csv` (`export_csv.py`), exposed on `/query`
> (`hit.cli_security`), and wired into `RUN.sh` as opt-in step 8.5
> (`--with-cli-scan`). The [Assessment section](#assessment-of-the-prototype)
> records what the prototype got right and the three bugs this fixed.

## Purpose

Many `SKILL.md` files tell the agent (or the user) to `npm install -g <x>` or
`pip install <y>` a third-party **command-line tool** as a prerequisite. That
tool then runs with the user's privileges, outside anything the skill's own text
is scanned for. This step answers, per skill: *which CLI tools does this skill
have you install, and do any of them carry known security advisories?*

It is deliberately **not** a scan of library dependencies a skill's code
imports — only packages named in an install command, filtered to those that
ship an executable (npm `bin`, PyPI console entry point). Library
`import`-graph auditing is a much larger job and is out of scope (see
[Out of scope](#out-of-scope)).

Manually triggered for now. Designed to be wired into `RUN.sh` as an opt-in
`--with-cli-scan` step later, the same way `--with-leaderboard` and the planned
`--with-scan` are.

## How it relates to the other scan systems

| | Vettd deterministic scan (`publish_scans.py`) | LLM threat scan ([`ARCHITECTURE_LLM_SCAN.md`](ARCHITECTURE_LLM_SCAN.md)) | **CLI / dependency scan (this doc)** |
|---|---|---|---|
| Question | is the SKILL.md text itself malicious / policy-violating | does an LLM threat-model flag the SKILL.md text | do the **external CLI tools the skill installs** have known CVEs |
| Engine | external `vettd` binary | `POST /scan[/skill]` → litellm/OpenRouter | npm registry + PyPI + OSV.dev REST, no LLM, deterministic |
| Input | the extracted skill *folder* | the `SKILL.md` `content` string | install-command lines grep'd out of every file in the skill |
| Where results live | `locations[].vettd_scan_findings` / `locations[].vettd_scan_publications` | top-level `llm_scan` payload field | **top-level `cli_security` payload field** (this doc) |
| Selection | every extracted skill in the batch | top-N by stars with a Vettd security finding | every skill that names ≥1 confirmed-CLI package in an install command (~10k of ~200k) |
| Determinism | deterministic | non-deterministic | deterministic given a fixed OSV/registry snapshot; results drift as advisories are published |

The three are independent — the CLI scan has no dependency on the other two and
vice versa. A single `/query` response can carry all three
(`hit.cli_security`, `hit.llm_scan`, `hit.locations[].vettd_scan_findings`).

## Data flow

```
search-raw/**/*.{md,py,sh,txt,yaml,yml,json,js,ts}
  │  find_install_mentions.py   — regex sweep for install verbs (npm/pip/pipx/yarn/pnpm/uv/…)
  ▼
work/install_mentions.log       — "<path-relative-to-search-raw>:<lineno>: <line>"   (gitignored)
  │  extract_packages.py {npm,pip} extract
  │     — only parses "trusted" command text: backtick-enclosed spans, or a line that
  │       starts with the install verb. Prose like "treat every skill like an npm install"
  │       is skipped. Package specs validated against an ecosystem regex.
  ▼
work/{npm,pip}_packages.csv      — package, mentions, example   (gitignored)
  │  extract_packages.py {npm,pip} classify   — + on-disk JSON cache (work/cache/registry/)
  │     npm:  registry.npmjs.org/<pkg>/latest  → has `bin`            → "cli"
  │     pip:  pypi.org/pypi/<pkg>/json         → "Environment :: Console" classifier → "cli"
  │     both: name ends -cli / starts cli- / description says "CLI" → "likely-cli"
  │           registry 404 / network error     → "unknown"  (excluded from the audit)
  ▼
work/{npm,pip}_packages_classified.csv        (gitignored)
  │  audit_packages.py   — OSV.dev POST /v1/query for every cli / likely-cli row
  │     + on-disk JSON cache (work/cache/osv/), retry with backoff on 429/5xx
  ▼
work/{npm,pip}_security_report.csv            — + vuln_count, max_severity, advisory_ids   (gitignored)
  │  map_to_skills.py    — re-parse the log, skill_id_util → skill dir that contains SKILL.md
  ▼
work/{npm,pip}_security_report_with_skills.csv — + skills_mentioning   (gitignored)
  │  build_cli_export.py
  ▼
  ├─ (default)  set_payload("agent_skills", {"cli_security": {…}}, [point_id])  per matched skill
  └─ (--csv)    skills_export_cli.csv   — skills_export.csv rows + cli / cli_security_grade / cli_security_scan
                                          (kept only as a debugging / offline artifact)
```

All of `work/` is regenerable and gitignored. `run.sh` chains the seven steps.

## Target structure

```
search-demo/
  cli-security-scan/
    __init__.py
    _common.py                   # shared paths + cached, backoff-retrying JSON fetch
    find_install_mentions.py     # paths relative to search-raw/
    extract_packages.py          # `extract_packages.py {npm|pip} {extract|classify}` (classify folded in)
    audit_packages.py            # `audit_packages.py {npm|pip}` — OSV; cache + backoff
    map_to_skills.py             # `map_to_skills.py {npm|pip}`
    build_cli_export.py          # the cli_security verdict: Qdrant set_payload (default) or --csv
    skill_id_util.py             # moved verbatim from temp/ (git mv)
    run.sh                       # chains the six report steps; reuses the mentions log unless --refresh
    README.md                    # how to run + refresh (cost, drift, cadence)
    conftest.py / test_*.py      # hermetic unit tests (parser, grading, verdict assembly)
    .gitignore                   # work/
    work/                        # all intermediates + work/cache/ (gitignored)
  docs/ARCHITECTURE_CLI_SECURITY_SCAN.md   # this file
  experiments/
    novector_index_benchmark.py  # was temp/make_small_index_novectors.py — unrelated, see below
  smoke_cli_security_context7.py # repo root, alongside the other smoke_*.py
```

Rationale for a subdir (not root scripts): this is a multi-step pipeline with
its own intermediate artifacts and cache, matching `mcp-search/`'s shape rather
than the single-file root tools (`export_csv.py`, `publish_scans.py`).

### `temp/make_small_index_novectors.py` — not part of this

It's a one-off benchmark comparing a payload-only Qdrant collection against the
vector-backed `qdrant_db_small/` on disk-size and query latency. It has nothing
to do with CLI security; it was only ever in `temp/` because that's where the
prototype session put its scratch files. Move it to `experiments/` (new dir) as
`novector_index_benchmark.py` and leave it there. The
`temp/qdrant_db_small_novectors/` snapshot it writes is:

- **regenerable** — one `uv run` of the script rebuilds it from `qdrant_db_small/`
- **already gitignored** (`qdrant_db_*/`)
- **not present on disk** in this checkout

Nothing about it needs keeping. If someone wants the benchmark numbers
preserved, paste the script's stdout into a comment at the top of the file; the
snapshot itself is disposable.

## The `cli_security` payload field

One object per point (per unique `SKILL.md` content), latest only, no history:

```json
{
  "cli_security": {
    "scanned_at": "2026-08-30T12:00:00+00:00",
    "osv_snapshot_date": "2026-08-30",
    "grade": "C",
    "packages": [
      {
        "package": "playwright",
        "ecosystem": "npm",
        "classification": "cli",
        "install_command": "npm install -g playwright && npx playwright install chromium",
        "vuln_count": 2,
        "max_severity": "HIGH",
        "advisory_ids": ["GHSA-xxxx-xxxx-xxxx", "GHSA-yyyy-yyyy-yyyy"]
      }
    ]
  }
}
```

- **`grade`** — `A` / `B` / `C`, the **worst** grade across `packages[]`:
  - `A` — package has no known OSV advisory
  - `B` — worst advisory is `LOW` or `MODERATE`/`MEDIUM`
  - `C` — worst advisory is `HIGH` or `CRITICAL`, **or** the package has an
    advisory OSV gave no severity label for (treated conservatively as C — see
    the [known limitation](#grade-inflation) below)
- **`osv_snapshot_date`** — the date the OSV data was fetched. Grades are only
  comparable within the same snapshot; this makes drift auditable.
- **`install_command`** — the first literal install line seen for that
  (skill, package) pair, truncated to 200 chars. Recovered from the log, not
  reconstructed.
- Absent until a skill has been through the scan. A skill that names **no**
  confirmed-CLI package never gets the field (not `grade: "A"` — absence and
  "clean" are different states).

### Why "package has an advisory", not "this skill is vulnerable"

`audit_packages.py` queries OSV **without a version**, so OSV returns every
advisory ever filed against the package across all versions. Most install
commands don't pin a version (`npm i -g foo`, not `foo@1.2.3`), so there is no
version to check against. The grade therefore means *"a tool this skill tells
you to install has a security history"* — a prompt to review, not a proven
exposure. The `cli_security` object stores `advisory_ids` so a consumer can
resolve the actual affected ranges. This limitation is stated in the payload
field's doc entry and the README.

## Wiring into the pipeline

### `build_cli_export.py` — the only writer

Mirrors `publish_scans.py`'s shape: `Config.from_env()`, `preflight()` (open
Qdrant, confirm `agent_skills` exists, load the report CSVs), a batch driver,
a thin `main(argv)`. Self-contained `uv run --script` header.

1. Scroll `agent_skills` (`with_payload=["path","content_hash","locations"]`,
   `with_vectors=False`) — a full client-side scroll, the norm in this repo
   (`agent_skills` has no index on nested `locations[]`, so a server-side
   filter on them times out at scale; see `TEST_PLAN_FINDINGS_SUMMARY_TOP1000.md` §5).
2. For each point, derive `skill_id` from every `locations[].path` via
   `skill_id_util.skill_id_from_path` and union the confirmed-CLI packages
   mapped to those ids (from `*_security_report_with_skills.csv`).
3. Points with ≥1 package → build the `cli_security` object, then
   `client.set_payload("agent_skills", {"cli_security": {…}}, points=[id])`.
   This is the step's **only** Qdrant write — one key, via `set_payload`, never
   a vector.
4. **Rescan gate** (mirrors `publish_scans.py`): skip the write when the
   existing `cli_security.osv_snapshot_date` is today's date **and** the set of
   packages is unchanged. `--force` bypasses.
5. `--csv` instead writes `skills_export_cli.csv` (join onto `skills_export.csv`
   in memory, NUL-sanitized) and makes no Qdrant write — for offline
   inspection only.

### `index_qdrant.py` — preserve across re-index

`_preserve_scan_publications()` currently carries `vettd_scan_publications`
forward inside `locations[]`. Extend it (same change the `llm_scan` plan calls
for — coordinate if both land together):

- add `"cli_security"` (and `"llm_scan"`) to its `client.retrieve(...)`
  `with_payload` list
- after the per-location `vettd_scan_publications` copy, copy a stored
  top-level `cli_security` onto the incoming `SkillPayload` when the point id
  matches. Point id is `point_id(content_hash)`, so a preserved `cli_security`
  always matches the same install commands.

`--refresh-metadata` / `--metadata-only` (`set_payload` / `delete_payload`)
already leave unlisted fields untouched.

### `export_csv.py` — three derived columns

Add `cli`, `cli_security_grade`, `cli_security_scan` to `FIELDS`, derived from
the `cli_security` payload key:

- `cli` = `json.dumps([{package, ecosystem, install_command} …])`
- `cli_security_grade` = `cli_security.grade` (empty when the key is absent)
- `cli_security_scan` = `json.dumps([{package, ecosystem, vuln_count, max_severity, advisory_ids} …])`

This makes `skills_export.csv` carry the columns directly — the prototype's
separate `skills_export_cli.csv` is retired (still producible via
`build_cli_export.py --csv` for debugging).

### `RUN.sh` — opt-in `[8.5/9]` step

Between step 8 (index) and step 9 (CSV export), gated behind `--with-cli-scan`,
default skipped — same treatment as `--with-leaderboard` / `--with-search`.
Runs `cli-security-scan/run.sh` then `build_cli_export.py`. Reads the
*indexed* collection, so it runs once after the final index, never per batch.
`RUN.sh`'s header comment gets a bullet describing it, like the existing
`--with-scan` note.

### `app/` — expose on `/query` and retire the mock (optional follow-up)

- `app/search.py` `SkillPayload`: add `cli_security: dict | None = None` with
  a docstring pointing here (mirrors the existing `llm_scan` entry).
- `SkillHit` / `_to_skill_hit` in `app/query_service.py`: carry `cli_security`
  through.
- `docs/QUERY_INTERFACE.md` payload table: new row.
- `app/openapi.json`: regenerate.
- `pick_random_security_status()` — `docs/QUERY_INTERFACE.md` §"Current
  synchronization audit" already flags the randomized `SecurityStatus` as not
  part of the payload contract. Once `cli_security` (and `llm_scan`) are real,
  `SecurityStatus` can be derived from them or removed. Tracked there, not
  blocking this step.

## Assessment of the prototype

Reviewed at commit 3ef5daf. Verdict: **sound approach, three fixes needed
before it reproduces from a clean checkout.**

### Reproducibility — the prototype didn't reproduce from a clean checkout; fixed here

| Problem | Fix |
|---|---|
| **`build_cli_skills_csv.py` crashes on the committed data.** `skills_export.csv` contains 274 NUL bytes (upstream, in the `contacts` skill's `description`). Python's `csv` module raises `_csv.Error: line contains NUL`. Confirmed — the script cannot complete. | `build_cli_export.py` reads each line through a `.replace("\x00", "")` (or reads the row set from Qdrant, which never has the raw NULs, in `--emit-payload` mode). |
| **`install_mentions.log` is machine-specific.** Every line is an absolute `/Users/c/code/ah-skills/…` path from the prototype author's laptop. `skill_id_from_path` returns those strings unchanged (they aren't under this machine's `search-raw/`), while `build_cli_skills_csv` derives *relative* ids from `skills_export.csv` → **the join matches nothing** unless the log is regenerated locally. `find_install_mentions.py` writes `str(path)` from an already-`.resolve()`d dir, so it always emits absolute paths. | `find_install_mentions.py` writes `path.relative_to(SEARCH_DIR)`. Then log ids and export ids are both `search-raw`-relative and the join works on any machine. The 13 MB log stops being committed (gitignored under `work/`). |
| **No cache / no resumability.** A re-run re-fetches ~2,400 npm + ~1,600 PyPI + ~1,500 OSV requests every time (a ~45–70 min cold run at observed latency); a failure mid-run loses all progress. | `_common.cached_json` writes an on-disk JSON entry per `(kind, ecosystem, package)` under `work/cache/` — registry lookups and OSV both. A re-run is cache-served (seconds). HTTP errors (404) are cached too; transient 429/5xx get exponential-backoff retries and are not cached. `--refresh` ignores the cache; each entry carries its fetch date so "refresh advisories" is just `rm -rf work/cache/osv/ && ./run.sh`. |

### Parsing / classification — acceptable, minor noise

- `extract_npm_packages.py` over-captures `npx` arguments:
  `npx create-react-app myapp` yields both `create-react-app` and `myapp`;
  `npx playwright install chromium` yields `chromium`. Low volume. Fix: for
  `npx`, take only the first token and stop. Not blocking.
- `unknown`-classified packages (npm 54, pip 51 — registry 404 / timeout,
  common for scoped packages on `/latest`) are silently dropped from the audit.
  Acceptable; the cache + a `--audit-unknown` flag would let someone sweep them
  later.
- npm `bin`-field detection is a strong CLI signal. PyPI's
  `Environment :: Console` classifier is weaker (many real CLIs omit it) but the
  name/description fallback catches those as `likely-cli`, which the audit still
  includes.

### Grade rules — logic is correct, but the grade over-states risk

`grade_for_package` (A/no advisory, B/LOW-MODERATE, C/HIGH-CRITICAL-or-unlabeled)
and `worst_grade` across a skill's packages are both correct as written. Two
systematic biases toward **C**, both to be documented rather than "fixed"
(they're conservative on purpose):

1. <a id="grade-inflation"></a>**Version-blind.** OSV is queried with no
   version → every historical advisory counts, even ones fixed years ago in
   versions predating any realistic install. See
   [Why "package has an advisory"](#why-package-has-an-advisory-not-this-skill-is-vulnerable).
2. **Unlabeled severity → C.** OSV PyPI entries (PYSEC) frequently carry only a
   raw CVSS vector string, no `HIGH`/`MEDIUM` label. `audit_packages.severity_label`
   returns the vector string, which `SEVERITY_ORDER` ranks as 0, so
   `grade_for_package` hits its "advisory present, severity unrecognized → C"
   branch. This makes pip-heavy skills skew C. Option for later: parse the CVSS
   vector to a base score → band.

### Error handling / rate limiting

The prototype used `time.sleep(0.05)` (registries) / `0.1` (OSV), recorded
errors per-row, and did not retry. Now in `_common._fetch`: a 3-try
exponential backoff on 429/500/502/503/504, a 0.05 s courtesy sleep only on
cache-miss traffic, and HTTP 404s cached (a private/renamed package stays 404)
while transient failures are not. Resumability comes from the cache, not retry.

### `skills_export_cli.csv` (was committed)

Carried the same 274 NUL bytes inherited from `skills_export.csv`. The doubled
`""` inside the `cli` column is correct CSV escaping of embedded JSON, not
corruption. Dropped from version control; `build_cli_export.py --csv`
regenerates it into `work/skills_export_cli.csv` (with a `.replace("\x00","")`
on every input line).

## Config

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SKILLS_QDRANT_DB_PATH` / `SKILLS_QDRANT_URL` | one, not both | — | Qdrant selection, same as `search.py` / `publish_scans.py`. `build_cli_export.py` **writes** the `cli_security` key. |

No secrets — every API used (npm registry, PyPI, OSV.dev) is unauthenticated.
Timeout (10 s) and retry count (3) are `_common.DEFAULT_TIMEOUT` /
`DEFAULT_MAX_RETRIES`; the same-day rescan gate is bypassed with
`build_cli_export.py --force`, not an env var.

## Running it

```bash
cd search-demo

# 1. Qdrant up, agent_skills indexed
uv run python stats.py

# 2. Build the reports (grep → extract → classify → audit → map). Cold run is
#    ~40–70 min — one npm-registry / PyPI / OSV call per unique package
#    (~4k packages) — but every response is cached under work/cache/, so a
#    re-run is seconds. The search-raw/ sweep in step 1 is also reused unless
#    --refresh. Writes cli-security-scan/work/.
cli-security-scan/run.sh

# 3. Write cli_security onto every matched skill point
uv run python cli-security-scan/build_cli_export.py
#    …or, offline CSV instead of a Qdrant write:
uv run python cli-security-scan/build_cli_export.py --csv

# 4. See it on a query
curl -sS localhost:8000/query -H 'Content-Type: application/json' \
  -d '{"query":"browser automation playwright","asset_type":"skill","limit":5}' \
  | python3 -c 'import json,sys; [print(h["name"], h.get("cli_security",{}).get("grade")) for h in json.load(sys.stdin)["hits"]]'
```

## Refreshing

The scan drifts as advisories are published. To refresh:

```bash
rm -rf cli-security-scan/work/cache/osv/     # keep the registry cache (packages don't change ecosystem)
cli-security-scan/run.sh
uv run python cli-security-scan/build_cli_export.py --force
```

`--force` is needed because the rescan gate skips a point whose package set is
unchanged even when severities moved. A monthly cadence matches how fast the
npm/PyPI advisory databases move for the long tail of CLI tools.

## Verification

Run and confirmed during incorporation (2026-08-30):

- **Report pipeline, full run over `search-raw/`** (85k skills): `run.sh` →
  npm 2407 packages / 1083 audited / 102 with advisories; pip 1589 / 276
  audited. Every response cached under `work/cache/` (~5.4k entries); a re-run
  is seconds. `@upstash/context7-mcp` → classified `cli`, OSV-clean, mapped to
  `yeachan-heo/oh-my-claudecode/skills/mcp-setup` (the skill whose SKILL.md has
  `claude mcp add context7 -- npx -y @upstash/context7-mcp` — a shape the
  prototype's parser missed entirely).
- **`build_cli_export.py --csv`**, real report data + committed
  `skills_export.csv` (the NUL-byte file that crashed the prototype): 12,330
  skill rows, grades A 10818 / B 238 / C 1274, no crash.
- **`build_cli_export.py` payload path**: hermetic E2E test (in-memory Qdrant)
  — write, worst-grade-across-packages, same-day rescan gate, `--force`,
  clear-on-no-longer-matching, `--dry-run`.
- **`_preserve_scan_publications`**: hermetic tests — `cli_security` carried
  across a re-index; not leaked to a content-changed (new-id) point.
- **`export_csv._apply_cli_security`** + full `app/` suite (34 passed,
  `openapi.json` regenerated) + `smoke_cli_security_context7.py` (live npm +
  OSV).
- **`uv run pytest cli-security-scan/`** — 26 passed.

Not run here (no populated `agent_skills` index on the box): the payload write
+ `export_csv.py` + `/query` against a **real large** index. Steps when one
exists:

1. `build_cli_export.py` → `wrote N / skipped 0`; re-run same day →
   `skipped N`; `--force` → `wrote N`.
2. Retrieve written ids `with_payload=["cli_security"]` — `grade` ∈ {A,B,C},
   `packages[]` populated, `install_command` recovered.
3. Re-run `index_qdrant.py` → re-retrieve → `cli_security` preserved.
4. `export_csv.py` → the three columns populated for exactly those skills.
5. `curl :8000/query …` → a CLI-installing skill's hit carries `cli_security`.
6. Spot-check 5 grades against the OSV web UI.

> **Known pre-existing failure, unrelated to this work:**
> `test_index_qdrant_publications.py` has 4 tests failing on `main` (a stale
> `load_skills` monkeypatch — `unexpected keyword argument 'ranked_only'`).
> The 5 receipt-preservation tests and the 2 added here pass.

## Out of scope

- **Library / import-graph auditing** — only packages named in an install
  command, and only those that ship an executable.
- **MCP servers** — the `mcp_servers` collection has its own dependency-vuln
  pass (`mcp-search/fetch_mcp_security.py`, the `security_direct_deps_*`
  payload fields). Not touched here.
- **Version resolution** — grades are package-level, not version-level (see
  [Why "package has an advisory"](#why-package-has-an-advisory-not-this-skill-is-vulnerable)).
- **Non-npm/pip ecosystems** — `find_install_mentions.py` also matches
  `cargo`/`brew`/`go`/`gem`/`apt` lines, but only npm and pip are extracted,
  classified, and audited. The log keeps the rest for a future pass.
- **Verdict history** — latest `cli_security` only.
