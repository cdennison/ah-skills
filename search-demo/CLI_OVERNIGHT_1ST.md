# CLI_OVERNIGHT_1ST — first full run: `cli_security` on every CLI-installing skill

**Goal.** Get a `cli_security` OSV verdict onto **every skill point in
`agent_skills` that installs a confirmed CLI tool** — the whole collection,
not a sample. This is the first end-to-end run of the
[`cli-security-scan/`](cli-security-scan/) pipeline against a freshly
re-cloned, freshly re-indexed corpus.

Pipeline design: [`docs/ARCHITECTURE_CLI_SECURITY_SCAN.md`](docs/ARCHITECTURE_CLI_SECURITY_SCAN.md).
Standard maintenance workflow this builds on: [`DAILY_JOB.md`](DAILY_JOB.md),
[`RUN.sh`](RUN.sh).

---

## What "done" looks like

- [ ] `agent_skills` re-indexed from a re-cloned `search-raw/` (registry is
      ~2 weeks stale as of writing — everything shows `last_synced=2026-08-14`).
- [ ] `cli-security-scan/run.sh` completed: `work/{npm,pip}_security_report_with_skills.csv`
      present, `work/cache/` populated (~5k entries).
- [ ] `build_cli_export.py` wrote `cli_security` onto every matched point
      (`wrote N / skipped 0` on the first pass).
- [ ] `skills_export.csv` regenerated — `cli_security_grade` populated for
      exactly those N skills.
- [ ] A spot `POST /query` for a known CLI-installing skill returns a
      `cli_security` object.
- [ ] `stats.py` clean; findings written to the run log.

Rough budget: **8–12 h wall-clock**, mostly the re-clone (~2 h, capped at
1000 clones/h) and the full embed (~3–6 h, CPU-bound). Start it before you
stop for the day.

---

## 0. Preflight (do this before starting — ~5 min)

```bash
cd search-demo

# tools + venv
command -v uv && command -v gh && test -x .venv/bin/python || uv sync

# credentials (.env)
grep -E '^GITHUB_PAT=.{20,}' .env            # must be non-empty; re-clone needs it
#   Qdrant: embedded mode is fine for this run. Leave SKILLS_QDRANT_URL unset
#   and SKILLS_QDRANT_DB_PATH unset -> defaults to ./qdrant_db. (See "Risks".)

# disk — the tight constraint on this box (50G, ~14G free at last check)
df -h .
#   Need headroom for: search-raw/ (~1.2G, already present) + qdrant_db/
#   (~1–3G for ~60–70k points w/ dense+sparse vectors) + one clone batch
#   (<1G, batch_pipeline stops if free space drops below 1G).
#   If free space < 6G: clear old scratch first —
#     rm -f *.log run_sh_output.log stats*.log index_finish*.log
#     rm -rf qdrant_db_test* qdrant_db_memtest* qdrant_db_agentcompat_test
#     (do NOT delete qdrant_db/ if you want to resume an interrupted index)

# network reachability for the scan step
curl -sS -o /dev/null -w '%{http_code}\n' https://registry.npmjs.org/npm/latest   # 200
curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/pip/json           # 200
curl -sS -X POST https://api.osv.dev/v1/query -d '{"package":{"name":"lodash","ecosystem":"npm"}}' -o /dev/null -w '%{http_code}\n'  # 200

# nothing else holding the pipeline lock
ls -d .run.lock.d 2>/dev/null && echo "LOCK PRESENT — is another run going? remove only if sure"
```

**Decide: re-clone or not.**

| Situation | Action |
|---|---|
| Registry stale (default here — `registry.py unsynced` lists ~everything) or you want fresh repos | **Full path** (§1) — re-clone + re-extract + re-index |
| `search-raw/` already current and you only need the index rebuilt | Skip to §2 (`index_qdrant.py`) — no clone |
| `agent_skills` already fully indexed and current | Skip to §3 (scan only) |

On this box right now: **full path** — `qdrant_db/` is empty and the last
sync was 2026-08-14.

---

## 1. Re-clone + re-extract + re-index + scan + CSV — one command

`RUN.sh --with-cli-scan` chains every step in order (see its header for the
full 1–9 + 8.5 list). Run it detached with a log:

```bash
cd search-demo
nohup ./RUN.sh --with-cli-scan > cli_overnight_1st.log 2>&1 &
echo $! > cli_overnight_1st.pid
```

What it does, with what to expect:

| Step | What | Expect | If it stalls / fails |
|---|---|---|---|
| 1–3 | `refresh_seeds.py`, `registry.py sync-seed`, `fetch_marketplace.py` | seconds–2 min; additive registry updates | network to github.com; safe to re-run |
| 4–6 | leaderboard / search — **skipped** (no `--with-leaderboard` / `--with-search`) | one line each | — |
| 7 | `batch_pipeline.py --only-unsynced --skip-index` — clone+extract, 50 repos/batch, `repos/` wiped between batches | **~1.5–2.5 h** for the full stale registry (~1700 repos, 1000 clones/h cap → it *sleeps* at the cap, doesn't error). Per-batch progress lines. Stops cleanly if free disk < 1 G. | re-run `RUN.sh --with-cli-scan` — `--only-unsynced` resumes by content, only un-stamped repos get re-walked |
| 8 | `index_qdrant.py --batch-size 10000` — embed `search-raw/` into `qdrant_db/` | **~3–6 h** first time (full embed of ~85k SKILL.md, CPU-bound). Live `tqdm` bar with ETA. `[warn] skipping non-file` lines are harmless. | re-run — incremental by filename, already-embedded paths are skipped with no read; a resumed run only does the remainder |
| 8.5 | `cli-security-scan/run.sh` + `build_cli_export.py` (this is the point of the night) | **~1–1.5 h**: search-raw sweep (~6–10 min) → classify ~4k npm+pip packages → OSV audit ~1.5k → map → `set_payload` `cli_security` on ~10–15k points. Every HTTP response cached under `cli-security-scan/work/cache/`. | **non-fatal** — RUN.sh logs the failure and still does step 9. Re-run just this part by hand (§3) afterward; the cache makes the retry fast |
| 9 | `export_csv.py` ×2 | minutes; `skills_export.csv` + `skills_export_top.csv` | re-run standalone |

### Monitoring

```bash
tail -f cli_overnight_1st.log
# clone progress:
grep -c 'cloned\|extracted' cli_overnight_1st.log
# scan progress (once step 8.5 starts):
find cli-security-scan/work/cache -type f | wc -l      # climbs toward ~5000
grep -E '^\[(npm|pip)\]' cli_overnight_1st.log | tail
# is it still alive?
ps -p "$(cat cli_overnight_1st.pid)" >/dev/null && echo RUNNING || echo STOPPED
```

### If `RUN.sh` died partway

Just run it again — `nohup ./RUN.sh --with-cli-scan > cli_overnight_1st.log 2>&1 &`.
Every step is idempotent/resumable (clone by content, index by filename,
scan by cache + same-day gate). Remove a stale `.run.lock.d` **only** if
`ps` confirms nothing is running.

---

## 2. Manual fallback — re-index without a full `RUN.sh`

Use this if `search-raw/` is already current and you only need the index +
scan (skips §1's clone), or if you want tighter control per phase.

```bash
cd search-demo

# 2a. (only if re-cloning) clone + extract in bounded batches
nohup uv run python batch_pipeline.py --batch-size 50 --only-unsynced --skip-index \
  > clone_1st.log 2>&1 &

# 2b. full embed into qdrant_db/
nohup uv run python index_qdrant.py --batch-size 10000 > index_1st.log 2>&1 &
#   resumes on re-run; add --hash only if SKILL.md content changed at
#   already-indexed paths (not the case on a first run).

# 2c. verify the index before scanning
uv run python stats.py            # agent_skills point count > 0 and stable
```

---

## 3. The scan step, standalone (also the re-run path for §1 step 8.5)

```bash
cd search-demo

# 3a. build the reports (idempotent; cache-served on re-run)
nohup cli-security-scan/run.sh > cli_scan_1st.log 2>&1 &
#   watch: tail -f cli_scan_1st.log ; find cli-security-scan/work/cache -type f | wc -l

# 3b. dry-run first — see how many points would get cli_security, no writes
uv run python cli-security-scan/build_cli_export.py --dry-run
#   expect: "would write ~10000–15000 / skipped 0 / cleared 0"

# 3c. write it
uv run python cli-security-scan/build_cli_export.py
#   first pass:  "wrote N / skipped 0 / cleared 0"
#   re-run same day, unchanged: "wrote 0 / skipped N"

# 3d. regenerate the flat CSVs so the columns are populated
uv run python export_csv.py
uv run python export_csv.py --ranked-only --limit 50000
```

---

## 4. Verify (~10 min)

```bash
cd search-demo

# point count + grade distribution straight from the CSV
python3 - <<'EOF'
import csv, collections
csv.field_size_limit(10**7)
g = collections.Counter()
n = 0
with open('skills_export.csv', newline='') as f:
    for row in csv.DictReader((ln.replace('\x00','') for ln in f)):
        if row.get('cli_security_grade'):
            g[row['cli_security_grade']] += 1; n += 1
print(f"{n} skills graded:", dict(g))
EOF
#   sanity: n is in the ~10k–15k range; A >> C > B (see the doc — C is
#   deliberately conservative: version-less OSV + unlabelled severities).

# a live query carries the verdict
( cd app && nohup uv run uvicorn query_service:app --host 127.0.0.1 --port 8012 > /tmp/qsvc.log 2>&1 & )
sleep 8
curl -sS localhost:8012/query -H 'Content-Type: application/json' \
  -d '{"query":"cloudflare workers wrangler deploy","asset_type":"skill","limit":5}' \
  | python3 -c 'import json,sys; [print(h["name"], "->", (h.get("cli_security") or {}).get("grade")) for h in json.load(sys.stdin)["hits"]]'

# the context7 smoke test (live npm + OSV, one package)
uv run python smoke_cli_security_context7.py

# tests
uv run pytest cli-security-scan/ -q
( cd app && uv run pytest -q )

uv run python stats.py    # nothing regressed
```

Write the numbers (indexed point count, N graded, grade split, run
durations, anything that broke) into `cli_overnight_1st.log` or a short
`CLI_OVERNIGHT_1ST_RESULTS.md`.

---

## 5. After a good run

```bash
cd search-demo

# refresh the shared data bundle so others skip the re-clone/re-index
./make_data_zip.sh
gh release upload <tag> search_demo_data.zip --clobber   # or: gh release create <tag> ...

# tidy scratch logs (all gitignored, but keep the box clean)
rm -f clone_1st.log index_1st.log cli_scan_1st.log cli_overnight_1st.pid
#   keep cli_overnight_1st.log / CLI_OVERNIGHT_1ST_RESULTS.md until reviewed
```

Nothing new to commit from the run itself — `repos/`, `search-raw/`,
`qdrant_db/`, `skills_export*.csv`, and `cli-security-scan/work/` are all
gitignored. The code + this plan are committed separately (the CLI-security
incorporation commit).

---

## Risks / gotchas for a 1ST run

- **Embedded Qdrant at scale.** `qdrant_db/` past ~20k points prints its own
  slowness warning; opening a ~60–70k-point store costs ~1–2 min per process
  (see `app/search.py`). Fine for this batch run, but if `/query` latency or
  concurrent reindex+serve becomes a problem, switch to the Docker server:
  `docker/deploy-qdrant-docker.sh`, then set `SKILLS_QDRANT_URL=http://localhost:6333`
  and unset `SKILLS_QDRANT_DB_PATH` in `.env` (see `docs/QUERY_INTERFACE.md`).
- **Disk.** ~14 G free is enough but not generous. `batch_pipeline.py`
  self-stops below 1 G (finishing the in-flight batch); if it does, free
  space and re-run. Watch `df -h .` during step 8.
- **OSV / registry throttling.** `_common._fetch` retries 429/5xx with
  exponential backoff and a 0.05 s courtesy sleep on cache misses. A cold
  run is ~4k+1.5k calls; if a provider hard-throttles, the affected packages
  land as `unknown` / `vuln_count="?"` and are simply excluded — re-run §3
  later (cache-served) to fill them in.
- **The grade is conservative by design.** `cli_security.grade` = "a CLI
  this skill installs has a security *history*", not "you are vulnerable":
  OSV is queried version-less, and PyPI advisories with no severity label
  fall to C. Don't over-read the C count. `advisory_ids` on each package is
  the real detail.
- **Re-clone picks up registry churn.** Steps 1–3 of `RUN.sh` add repos
  (marketplace, awesome-list). New repos → new skills → they get indexed and
  scanned in the same run. Expected, not a problem; just note the point
  count will differ from the last known number.
- **`test_index_qdrant_publications.py` has 4 pre-existing failures on
  `main`** (stale `load_skills` monkeypatch — unrelated to this work). The
  `cli-security-scan/` suite and the 2 added `cli_security` preservation
  tests pass.
