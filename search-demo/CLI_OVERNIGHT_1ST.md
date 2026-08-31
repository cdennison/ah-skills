# CLI_OVERNIGHT_1ST — first full run: `cli_security` on every CLI-installing skill

**Goal.** For **every skill point in `agent_skills` that installs a confirmed
CLI tool** — the whole collection, not a sample:

1. a `cli_security` OSV advisory verdict on the *packages* it installs
   (§1–§3, the [`cli-security-scan/`](cli-security-scan/) pipeline), and
2. a `vettd` deterministic scan of the skill *folder* itself
   (§3B, `publish_scans.py`).

Against a freshly re-cloned, freshly re-indexed corpus.

Design: [`docs/ARCHITECTURE_CLI_SECURITY_SCAN.md`](docs/ARCHITECTURE_CLI_SECURITY_SCAN.md)
(OSV) and [`docs/ARCHITECTURE_PUBLISHING_SCANS.md`](docs/ARCHITECTURE_PUBLISHING_SCANS.md)
(Vettd). Standard maintenance workflow this builds on:
[`DAILY_JOB.md`](DAILY_JOB.md), [`RUN.sh`](RUN.sh).

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
- [ ] **Vettd deterministic scan** run over every one of those N
      CLI-installing skills (§3B) — each has a `vettd_scan_publications`
      receipt for the configured target, or is in the failure log for retry.
- [ ] A spot `POST /query` for a known CLI-installing skill returns a
      `cli_security` object **and** `locations[].vettd_scan_findings`.
- [ ] `stats.py` clean; findings written to the run log.

Rough budget: **8–12 h** for the re-clone + embed + OSV scan (§1), then the
Vettd pass (§3B) is **on top** — potentially another 6–14 h at full scale
and unverified there (see §3B). It is resumable across nights (the 7-day
rescan gate means a re-run only picks up skills not yet scanned), so it's
fine to let §1 finish tonight and start §3B tomorrow, or cap §3B's first
slice. Start §1 before you stop for the day.

---

## Batched driver — `dual_scan_batched.py`

For a run you want to watch batch-by-batch with a both-scans check after
each, `cli-security-scan/dual_scan_batched.py` wraps §3 + §3B:

```bash
uv run python cli-security-scan/build_cli_export.py         # §3 — write cli_security
nohup uv run python cli-security-scan/dual_scan_batched.py --batch 500 \
  > cli-security-scan/work/dual_scan_run.log 2>&1 &         # §3B, batched + verified
```

Each batch runs `publish_scans.py` over ~500 CLI-installing skill folders,
then re-reads every point under them and asserts **both** `cli_security` and
a `vettd_scan_publications` receipt are present. One JSON line per batch to
`work/dual_scan_progress.jsonl`; per-batch scanner output in
`work/vettd_batch_logs/`. Resumable (folders with a receipt are skipped),
aborts if 3 batches in a row come back mostly unscanned. Folders whose
SKILL.md was content-deduped into another point (no own point to hang a
receipt on) are listed in `work/dual_scan_no_skillmd_point.txt` and skipped
— currently ~96 of ~11.2k.

Monitor:

```bash
tail -f cli-security-scan/work/dual_scan_run.log
python3 -c 'import json;[print(l.strip()) for l in open("cli-security-scan/work/dual_scan_progress.jsonl")]' | tail
```

> **The index already exists.** A Qdrant *server* is running on `:6333` with
> `agent_skills` = 62,329 points (that's what `get_client()` connects to when
> no `SKILLS_QDRANT_*` env is set — the empty embedded `./qdrant_db` is a
> leftover, not what the pipeline uses). So §1's re-clone/re-index is
> **optional** — only needed to refresh stale content or add new repos. The
> first `cli_security` + Vettd pass runs straight against the existing 62k.
> 12,431 points got `cli_security`; 11,141 of those are Vettd-scannable
> folders.

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

## 3B. Vettd deterministic scan of every CLI-installing skill

`cli_security` (§3) is the OSV advisory check on the *packages* a skill
installs. This is the **other** scanner — `publish_scans.py` runs the
`vettd` binary over each skill *folder* and submits the result to the Vettd
backend, landing a `vettd_scan_findings` / `vettd_scan_publications` block on
the skill's `locations[]` entry. Design:
[`docs/ARCHITECTURE_PUBLISHING_SCANS.md`](docs/ARCHITECTURE_PUBLISHING_SCANS.md),
[`DAILY_JOB.md` §5](DAILY_JOB.md).

Here we point it at exactly the skills that install a CLI — the same set §3
graded — rather than the whole corpus.

### Preconditions

- §1/§2 done: `agent_skills` indexed (`publish_scans.py` finds each skill's
  point to attach the receipt; unindexed skills are skipped).
- §3 done: the `cli_security` payloads exist (that's how we enumerate "skills
  with a CLI").
- **The Vettd backend is reachable.** `.env` has it at
  `VETTD_SCAN_ENDPOINT=http://localhost:3000/...` with
  `VETTD_CLI_BIN=/home/ec2-user/vettd-cli/target/release/vettd`. That's a
  **local dev backend** — it must be running. Start it however that repo
  starts it, then:

  ```bash
  cd search-demo
  set -a; . ./.env; set +a
  "$VETTD_CLI_BIN" --version
  "$VETTD_CLI_BIN" auth --key "$VETTD_API_KEY" --endpoint "$VETTD_SCAN_ENDPOINT" --allow-public-endpoint
  "$VETTD_CLI_BIN" auth status --json    # endpoint + account email must match .env
  curl -sS -o /dev/null -w '%{http_code}\n' "$VETTD_SCAN_ENDPOINT"   # reachable
  ```

  `publish_scans.py preflight()` re-checks all of this and aborts before
  scanning anything if it doesn't verify.

- **Raise the Vettd backend's `scans-ingest` rate limit for the run.** The
  Vettd API rate-limits `POST /api/scans/ingest` to **5 requests / 60 s per
  user** (`RATE_LIMIT_POLICY["scans-ingest"]` in
  `~/vettd/packages/api/src/rate-limit/policy.ts` — `limit: 5`,
  `windowMs: 60_000`, `scope: "user"`). A batch run (`publish_scans.py` /
  `dual_scan_batched.py`) fires far more than that and every skill past the
  first 5 in a window comes back **HTTP 429** — the receipt is dropped and
  the skill lands in the retry log, so a naive run "completes" having
  persisted almost nothing.

  For a local batch run, bump it — **local checkout only, do not commit**
  (the comment in `policy.ts` says so):

  ```ts
  "scans-ingest": {
      key: "scans-ingest",
      limit: 9_999_999,   // was 5 — local batch-testing only, revert before committing
      windowMs: 60_000,
      tier: "durable",
      scope: "user",
  },
  ```

  Then make the running backend pick it up:
  - `next dev` (or `pnpm dev`): `pnpm --filter @vettd/api build` (the
    `packages/api` is compiled — the dev server does **not** rebuild it),
    then restart the dev server.
  - the box's `vettd-web-1` container (what `VETTD_SCAN_ENDPOINT=http://localhost:3000`
    points at): `cd ~/vettd && docker compose up -d --build web`.

  On this EC2 box the bump is already baked into the working tree
  (`git -C ~/vettd diff packages/api/src/rate-limit/policy.ts`) and into the
  `vettd:local` image. If you rebuild that image from a clean checkout, or
  run against a fresh `next dev`, re-apply it. Revert (`git checkout`) once
  the batch run is done so it never reaches a commit or a real deployment.

> **Scale + risk.** `publish_scans.py` has been verified by hand on a handful
> of real skills against a real local backend (all four rescan-gate branches),
> **but not at registry scale and not against a production key** (see
> `DAILY_JOB.md` §5 "Not yet verified"). Expect this to be the long pole:
> each skill is a folder hash + upload + scanner run. Budget **hours**. It is
> safe to interrupt.

### Run it

```bash
cd search-demo

# 1. enumerate every skill folder that got a cli_security verdict, straight
#    from Qdrant (authoritative — this is exactly the §3 set).
set -a; . ./.env; set +a
python3 - <<'EOF' > cli_skill_dirs.txt
import os
from qdrant_client import QdrantClient
url, path = os.environ.get("SKILLS_QDRANT_URL"), os.environ.get("SKILLS_QDRANT_DB_PATH")
client = QdrantClient(path=path) if path else QdrantClient(url=url or "http://localhost:6333")
dirs, offset = set(), None
while True:
    pts, offset = client.scroll("agent_skills", with_payload=["locations", "cli_security"],
                                with_vectors=False, limit=1000, offset=offset)
    for p in pts:
        pl = p.payload or {}
        if not pl.get("cli_security"):
            continue
        for loc in pl.get("locations") or []:
            path = (loc or {}).get("path")
            if path and path.endswith("SKILL.md"):
                dirs.add("search-raw/" + path.rsplit("/", 1)[0])
    if offset is None:
        break
for d in sorted(dirs):
    print(d)
EOF
wc -l cli_skill_dirs.txt          # expect ~10k–15k

# 2. sanity: every listed dir exists and holds a SKILL.md
missing=$(while read -r d; do [ -f "$d/SKILL.md" ] || echo "$d"; done < cli_skill_dirs.txt)
[ -z "$missing" ] && echo "all dirs OK" || { echo "MISSING:"; echo "$missing" | head; }

# 3. scan them. xargs batches so argv stays sane and a batch that exits
#    nonzero (any per-skill failure) doesn't abort the rest. publish_scans.py
#    also catches per-skill failures internally and logs them for retry.
nohup bash -c '
  set -a; . ./.env; set +a
  xargs -a cli_skill_dirs.txt -n 400 -r uv run python publish_scans.py
' > vettd_cli_1st.log 2>&1 &
echo $! > vettd_cli_1st.pid
```

### Monitoring

```bash
tail -f vettd_cli_1st.log
grep -c 'published\|accepted\|duplicate' vettd_cli_1st.log     # progress
grep -Ei 'fail|error' vettd_cli_1st.log | tail                 # failures (retried next run)
ps -p "$(cat vettd_cli_1st.pid)" >/dev/null && echo RUNNING || echo STOPPED
```

### If it doesn't finish / dies

**Just re-run step 3.** The rescan gate makes it self-resuming: a skill that
got a receipt in this run is inside the 7-day window and is skipped next
time; only the not-yet-scanned (and failed) skills get attempted. So a
first pass that covers, say, 60% of the list followed by a second run the
next night completes the set with no bookkeeping.

To deliberately cap the first slice (e.g. only the skills that scored a
`C` from OSV, or the top-stars ones), filter `cli_skill_dirs.txt` before
step 3 — grep the `C`-grade paths out of `skills_export.csv`, or
`head -n 2000 cli_skill_dirs.txt`.

`VETTD_RESCAN_INTERVAL_DAYS=0` in the environment forces a rescan of
everything every run — don't set that here; it defeats the resume behaviour.

If the failure log is full of **HTTP 429 / "rate limit exceeded"**, the
backend's `scans-ingest` limit wasn't raised — see the Preconditions bullet
above, bump `policy.ts`, rebuild `@vettd/api` (or the `web` image), restart,
and re-run.

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

# Vettd coverage of the CLI set (§3B): how many of the graded skills now
# carry a vettd receipt, and how many are still outstanding
set -a; . ./.env; set +a
python3 - <<'EOF'
import os
from qdrant_client import QdrantClient
url, path = os.environ.get("SKILLS_QDRANT_URL"), os.environ.get("SKILLS_QDRANT_DB_PATH")
c = QdrantClient(path=path) if path else QdrantClient(url=url or "http://localhost:6333")
have_cli = scanned = 0
offset = None
while True:
    pts, offset = c.scroll("agent_skills", with_payload=["locations", "cli_security"],
                           with_vectors=False, limit=1000, offset=offset)
    for p in pts:
        pl = p.payload or {}
        if not pl.get("cli_security"):
            continue
        have_cli += 1
        if any((loc or {}).get("vettd_scan_publications") for loc in pl.get("locations") or []):
            scanned += 1
    if offset is None:
        break
print(f"CLI skills: {have_cli}  |  with a vettd receipt: {scanned}  |  outstanding: {have_cli - scanned}")
#   outstanding > 0 -> re-run §3B step 3; it resumes via the rescan gate
EOF

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
rm -f clone_1st.log index_1st.log cli_scan_1st.log cli_overnight_1st.pid \
      vettd_cli_1st.pid cli_skill_dirs.txt
#   keep cli_overnight_1st.log / vettd_cli_1st.log / CLI_OVERNIGHT_1ST_RESULTS.md
#   until reviewed — and vettd_cli_1st.log until §3B's "outstanding" count is 0
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
- **Vettd scan (§3B) is the biggest unknown.** It's unverified at scale and
  against a production key (`DAILY_JOB.md` §5), it needs a local backend up
  on `:3000` for the whole pass, and it will likely outrun a single night.
  Treat it as its own multi-night effort: run §1 first, confirm `cli_security`
  is right, *then* start §3B. The 7-day rescan gate makes each subsequent run
  a pure continuation. If the backend or box can't take the full ~10–15k, cap
  the first slice (C-grade or top-stars) and widen later.
- **`publish_scans.py` exits nonzero if any skill failed.** Under `xargs`
  that's expected on a large run — it's a partial-result signal, not a stop.
  Check `vettd_scan_publications` receipts / the failure log, not the exit
  code; failed skills carry no receipt and are retried on the next run.
- **`test_index_qdrant_publications.py` has 4 pre-existing failures on
  `main`** (stale `load_skills` monkeypatch — unrelated to this work). The
  `cli-security-scan/` suite and the 2 added `cli_security` preservation
  tests pass.
