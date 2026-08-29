# Daily/recurring maintenance job

This is the recurring workflow for keeping `repo-seeds/registry.json` (the
pipeline's single source of truth) curated and the search index fresh. It's
written as four steps you can run in order, by hand or from a script/cron —
none of them require re-explaining the pipeline itself (see
[`README.md`](README.md#pipeline) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for that).

**[`RUN.sh`](RUN.sh) automates steps 3 (partially) and 4 of this workflow**
— repo discovery (`sync-seed`, marketplace, optional GitHub search/leaderboard)
and rerunning the pipeline, in one command. It does **not** automate steps 0-2
(reviewing what's unsynced, skipping noisy repos, blacklisting bad skills) —
those stay a human judgment call by design. Run `./RUN.sh` for the mechanical
part of a daily/recurring pass, then still walk steps 0-2 below yourself.

**Order of operations: everything below is a pre-flight step, to be done and
human-reviewed, *before* step 4 actually clones/indexes anything.** Concretely,
before you run `batch_pipeline.py` (or `./RUN.sh`, or its step 4/7-9):

1. Check step 0's `./registry.py unsynced` — including whether the registry's
   `last_synced` timestamps are even trustworthy on this machine (see the
   "stale local clone state" warning under Non-obvious issues below; a fresh
   checkout of this repo, e.g. a new box, can show everything as "synced"
   while `repos/`/`search-raw/` are actually empty).
2. Walk step 1 (registry review / skip) and step 2 (blacklist review).
3. Refresh every source in step 3 — `refresh_seeds.py` + `sync-seed`,
   `fetch_marketplace.py`, and if you're doing a deliberate full refresh
   rather than a quick daily pass, the manual-only ones too:
   `pull_leaderboard.py` + `add_skillsh_leaderboard.py`, and a
   `search_github.py` pass per query (see step 3(c) below) followed by a
   **human read of the results before approving anything** with
   `registry.py add-search --approve`.
4. Only after all of the above is done and reviewed, run step 4 (clone +
   extract + index + CSV export, by hand or via `./RUN.sh`).

Skipping straight to step 4 without this pass means you're cloning/indexing
against a registry that may be missing a day's (or more) worth of newly
discovered repos, or re-trusting `last_synced`/`.clone_state.json` timestamps
that no longer reflect what's actually on disk.

## 0. Check what didn't sync

Every registry entry carries a `last_synced` timestamp, stamped by
`batch_pipeline.py` per-batch (or `registry.py mark-synced`, called by
`archived/run_pipeline.sh`'s last step) on every repo that has a directory
under `repos/` once extract+index have both succeeded — i.e. "cloned AND
through RAG". Start here before reviewing:

```bash
./registry.py unsynced   # active repos not synced today (never synced, or stale)
```

A repo failing to sync (clone error, 404, etc.) also gets `last_sync_failure`
+ `last_sync_failure_reason` recorded — by `clone_repos.py` itself when the
`git clone` fails, or generically by `mark-synced` if a repo is missing from
disk for some other reason. `last_synced` is only ever touched by a
*successful* sync, so a repo that fails today keeps whatever `last_synced`
it last had (or none, if it's never succeeded) — the failure fields are
purely additive information, never a replacement for the success timestamp.
Check `last_sync_failure_reason` on anything `unsynced` turns up before
deciding whether to skip it (step 1) or investigate further.

## 1. Review the registry and mark low-quality repos "skip"

```bash
./registry.py list                       # eyeball everything
./registry.py skip owner/repo "reason, e.g. user feedback: mostly noise, not real skills"
./registry.py unskip owner/repo          # if you change your mind
```

Repos are **never deleted** by this review step — only marked
`status: "skip"` with a required reason, so the decision (and who/why) is
preserved. `registry.py remove` still exists, but reserve it for outright
mistakes (a typo'd owner/repo that was never real), not quality judgments.

**⚠️ Known gap: `skip` is currently schema-only.** `clone_repos.py`'s
`repo_pairs()` does **not** filter out `status: "skip"` entries yet, so
marking something skipped has *no effect* on cloning, extraction, or
indexing today. It's there to start capturing the decision now (e.g. from
recurring user feedback) so a future change to `repo_pairs()` can act on the
backlog of decisions already made. Don't rely on `skip` to actually reduce
what gets cloned/indexed until that's wired up.

## 2. Blacklist individual skills within a repo

Sometimes a whole repo is worth keeping but one specific skill in it isn't
(mislabeled, broken, low-quality, or user-reported as bad). This is a
separate mechanism from registry skip, and **is fully enforced**:

```bash
./blacklist.py add owner/repo/skills/some-skill/SKILL.md "reason, e.g. user feedback: irrelevant to our use case"
./blacklist.py remove owner/repo/skills/some-skill/SKILL.md
./blacklist.py list
```

`extract_search_raw.py` skips any blacklisted path when copying into
`search-raw/`, and deletes it from `search-raw/` if it was already copied
there before being blacklisted. `index_qdrant.py`'s existing hash-diff logic
then removes it from Qdrant on its next run (a file disappearing from
`search-raw/` looks the same to it whether the repo changed upstream or a
human blacklisted it).

**This only takes effect after you rerun `extract_search_raw.py` +
`index_qdrant.py`** (step 4) — it's not a live filter on the already-built
`qdrant_db/`.

## 3. Update the repo list (always additive, never deletes)

**Every source of skills has to be continually refreshed — this isn't
just a `sync-seed` thing.** Two different kinds of staleness are in play
here, and it's easy to fix one and still be silently stuck on the other:

1. **Registry staleness** — a repo already tracked hasn't been re-cloned
   recently (`last_synced`, step 0/4 handle this).
2. **Source staleness** — the *list a repo would be discovered from* is
   itself out of date, so a brand-new upstream repo is invisible no matter
   how often you re-run the discovery step against it.

(2) is the one that's easy to miss, because the discovery scripts all exit
`0` and print "0 new repos" whether nothing changed upstream or your local
view of upstream is just stale. Concretely, for `officialskills.sh`:
[`repo-seeds/awesome-agent-skills/README.md`](repo-seeds/awesome-agent-skills/README.md)
is a **vendored, point-in-time copy** of the upstream
[VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)
repo, tracked separately in
[`repo-seeds/repo_seeds.json`](repo-seeds/repo_seeds.json) (`last_pulled`,
distinct from any individual repo's `last_synced` in `registry.json`).
`registry.py sync-seed` only ever regex-scrapes github.com links out of
*that vendored copy* — it never talks to GitHub itself. If the vendored
copy is a week stale, `sync-seed` will happily run every day, exit `0`,
and never surface the six repos VoltAgent added upstream in the meantime.
**`refresh_seeds.py` is what actually re-clones the upstream repo and
overwrites the vendored copy** — run it before `sync-seed`, not instead of
it:

```bash
./refresh_seeds.py          # re-vendor every tracked seed list from upstream
./registry.py sync-seed      # THEN scrape the now-fresh vendored copy
./registry.py seeds          # check every seed list's last_pulled
```

`RUN.sh` chains `refresh_seeds.py` → `sync-seed` in that order automatically
every run, for exactly this reason.

The same "is the *source* itself fresh, not just the registry" question
applies to every channel, with a different answer per channel:

| Source | What can go stale | How it's refreshed |
|---|---|---|
| Seed lists (`officialskills.sh`, ...) | The vendored copy under `repo-seeds/` | `refresh_seeds.py`, run before `sync-seed` (see above) |
| Marketplace | Nothing — fetched live from Anthropic's repo every run | No separate refresh step needed; `fetch_marketplace.py` always gets current data |
| `skills.sh` leaderboard | The raw snapshot in `leaderboard-raw/` | `pull_leaderboard.py`, **manual-only** (see [step 4](#4-rerun-the-pipeline-clone--extract--index) and `RUN.sh`'s header) — `add_skillsh_leaderboard.py` only reads whatever snapshot already exists, same relationship `sync-seed` has to `refresh_seeds.py` |
| GitHub search | Nothing — each run queries the live API | No separate refresh step; running `search_github.py` again IS the refresh |
| Individual repos already in the registry | Their own clone on disk | `last_synced` / step 0 above, refreshed by `batch_pipeline.py` |

`RUN.sh` runs (a) and (b) below automatically every time (plus
`refresh_seeds.py` immediately before (b), per the above); (c) and (d) are
opt-in/manual since they need human judgment (c) or are inherently one-off
(d) — see `RUN.sh`'s header comment. Four independent ways new repos enter
the registry, all additive-only — none of them ever remove an existing
registry row:

```bash
# a) Anthropic's official Claude plugin marketplace -- no review needed,
#    it's already a curated Anthropic source
./fetch_marketplace.py

# b) The vendored awesome-agent-skills list -- picks up anything appended
#    to repo-seeds/awesome-agent-skills/README.md since the last sync.
#    Run refresh_seeds.py first (see above) or this only sees whatever
#    was vendored as of the last refresh, not what's upstream right now.
./refresh_seeds.py
./registry.py sync-seed

# c) GitHub search -- requires a human review step before anything is added.
#    Three recurring queries are tracked, each run with both `stars` and
#    `best-match` sort (six runs total); `agent skills` also uses --exact,
#    matching how each was originally run. Output goes to
#    repo-seeds/search-runs/<query-slug>-<sort>.json, overwriting the
#    previous snapshot for that query+sort each time -- these files are a
#    point-in-time review queue, not an append-only history. (If you ever
#    need to confirm exactly which query/sort/exact combo originally found
#    a given repo, check the `search` descriptor(s) on its registry entry --
#    `registry.py list` or repo-seeds/registry.json directly.)
./search_github.py "agent skills"  --exact --sort stars      --top 100 --format json --out repo-seeds/search-runs/agent-skills-stars.json
./search_github.py "agent skills"  --exact --sort best-match --top 100 --format json --out repo-seeds/search-runs/agent-skills-best-match.json
./search_github.py "claude skills" --sort stars      --top 100 --format json --out repo-seeds/search-runs/claude-skills-stars.json
./search_github.py "claude skills" --sort best-match --top 100 --format json --out repo-seeds/search-runs/claude-skills-best-match.json
./search_github.py "codex skills"  --sort stars      --top 100 --format json --out repo-seeds/search-runs/codex-skills-stars.json
./search_github.py "codex skills"  --sort best-match --top 100 --format json --out repo-seeds/search-runs/codex-skills-best-match.json
# (read each file yourself, then approve per-file, e.g.:)
./registry.py add-search repo-seeds/search-runs/agent-skills-stars.json \
    --approve owner/repo --approve owner2/repo2
# ...repeat add-search against whichever of the six files have repos you approve

# d) One-off manual add, always with a reason
./registry.py add-manual owner/repo "found it linked from a blog post"
```

All four are safe to run repeatedly (idempotent), and — importantly — none
of them skip a repo just because it's *already in the registry from a
different channel*. **Overlap between channels is expected and wanted, not
something to dedupe away.** Each registry row has a `sources` list; a repo
already tracked via `seed` that also turns up in the marketplace gets a
second, `marketplace` descriptor added to the *same* row (`registry.py
list` then shows it as `[seed+marketplace]`) — it does not get skipped, and
it does not get a duplicate row. `fetch_marketplace.py` and `registry.py
sync-seed` both print which repos were genuinely new vs. newly-overlapping
so you can see this happening:

```
$ ./fetch_marketplace.py
obra/superpowers already tracked -- also found in marketplace as 'superpowers' (now seed+marketplace)
0 new repo(s), 9 newly-overlapping repo(s)
```

Re-running the same channel again on a repo it already found (e.g. running
`fetch_marketplace.py` twice in a row) just refreshes that one descriptor's
detail in place — it doesn't add a second `marketplace` entry to the list,
and it never touches descriptors from *other* channels, or a repo's `skip`
status.

**This overlap tracking is repo-level bookkeeping only — it has zero effect
on cloning or indexing volume.** `clone_repos.py` clones each `owner/repo`
exactly once no matter how many sources list it (the registry has one row
per repo, full stop), and `extract_search_raw.py`/`index_qdrant.py` produce
exactly one Qdrant point per `SKILL.md` path found on disk, regardless of
how many registry sources led to that repo being cloned. `sources` answers
"where did we hear about this from," never "how many times is this
indexed" — that's always once.

## 4. Rerun the pipeline (clone+extract in batches of 50 → index in batches of 10K → CSV)

**As of now, cloning/extraction and indexing are two separate, separately
batched steps** — not one `--stats` run that does both per-batch. This is so
each phase shows clear, granular progress instead of one opaque run (cloning
is network-bound and can have thousands of small steps; indexing is
CPU-bound and used to run as one silent multi-hour call). `RUN.sh` runs both
in this order automatically (steps 7-9); by hand it's:

```bash
# 1) clone + extract only, 50 repos per batch, no indexing yet
uv run python batch_pipeline.py --batch-size 50 --only-unsynced --skip-index

# 2) index search-raw/ into Qdrant, 10,000 skills per embed/upload call
uv run python index_qdrant.py --batch-size 10000

# 3) regenerate both CSVs
uv run python export_csv.py
uv run python export_csv.py --ranked-only --limit 50000
```

**Step 1 — clone + extract, 50 repos at a time:**
`clone_repos.py` on its own clones *everything* matching into `repos/` (full
git clones) before `extract_search_raw.py` ever runs — for the full
registry that's several GB+ sitting on disk at once, and has filled the
disk before. `batch_pipeline.py` clones in bounded batches, extracts each
batch into `search-raw/` (the only thing that actually needs to persist),
then deletes `repos/` via `clean_repos.sh` before the next batch — so
`repos/` never holds more than one batch's clones regardless of registry
size. It also checks free disk space before starting each new batch and
stops cleanly (finishing the in-flight batch's extract+clean-repos steps
first) if free space drops below 1GB.

- `--only-unsynced` limits it to repos step 0's `unsynced` check would
  flag, and skips by content (not registry position) so it correctly picks
  up the remaining backlog on a resumed run.
- `--skip-index` is the default expectation now — indexing happens
  separately in step 2, not per-batch here. (`--stats` still exists and
  still indexes every batch if you specifically want that old behavior —
  e.g. a small one-off run where you want sync+index+CSV counts in
  `stats.log` together — but it's no longer the standard path.)
- `--batch-size N` (default 50) — the standard size going forward, chosen
  so each batch is small enough to show meaningful progress; smaller still
  for a slow/careful catch-up or debugging, larger if the delta is small
  and you just want it done.
- **Self-imposed cap: 1000 clones/hour**, well under GitHub's own quota,
  enforced by `clone_repos.py`'s `enforce_hourly_cap()` — it sleeps once the
  cap is hit rather than erroring. This is tracked off `.clone_state.json`'s
  per-repo timestamps (not an in-memory counter), so the cap correctly
  persists across `batch_pipeline.py`'s one-subprocess-per-batch invocations
  instead of resetting every batch.

**Step 2 — index, 10,000 skills at a time:**
`index_qdrant.py` reads only from `search-raw/` and `registry.json` — it
has no dependency on `repos/` still existing, so it's safe to run any time
after step 1's batches have cleaned up their clones. By default it uses a
**fast filename-based check**: any `owner/repo/path` already recorded on an
existing point is skipped entirely (no read, no hash) and only genuinely
new files get embedded — this is what makes a large backlog affordable to
index at all, instead of re-hashing every file in `search-raw/` on every
run. The tradeoff: it won't catch a file whose *content* changed at a path
that was already indexed — pass `--hash` for the older, slower full
content-hash diff when that matters (e.g. after editing an already-indexed
`SKILL.md` in place, or after a blacklist change per step 2 above).

- `--batch-size N` (default 10000) — skills per `upload_collection` call.
  Each batch commits independently and prints a live `tqdm` progress bar
  with an ETA, so long runs are no longer a silent multi-hour black box.
  Lower it for a smoother/finer-grained bar; there's no other downside to a
  smaller size beyond slightly more overhead per call.
- `--metadata-only` skips content extraction/embedding entirely and just
  re-derives stars/sources/ranking on points already indexed, from
  `registry.json` — use this after a ranking-only change (e.g. `registry.py
  update-skillsh`, or a fresh `add_skillsh_leaderboard.py` run) with no new
  skill content to embed. Finishes in seconds even at 100k+ points.

**Step 3 — CSVs:** `export_csv.py` regenerates `skills_export.csv` (every
point). `export_csv.py --ranked-only --limit 50000` regenerates
`skills_export_top.csv` — only rows with non-empty `ranking` data, sorted by
best (lowest) numeric `*_rank` value across every source, capped at 50,000
rows. Use `--output PATH` on either to write somewhere else instead.

It's safe and cheap to run multiple times a day:

- **`clone_repos.py`** (invoked once per batch) — has its own per-repo 24h
  skip (`.clone_state.json`); repos cloned within the last day are skipped
  with no GitHub API call at all, so re-running costs almost nothing beyond
  newly-added repos.
- **`extract_search_raw.py`** — a full rescan of the *current batch's*
  `repos/` every time (not incremental across the whole registry), so it's
  seconds even at thousands of files.
- **`index_qdrant.py`** — incremental by filename (default) or content hash
  (`--hash`); either way a same-day rerun with nothing new finishes almost
  instantly, since already-known paths are skipped without being read.
- **`mark_synced_pairs()`** (called internally by `batch_pipeline.py` after
  each batch's clone+extract) — stamps `last_synced` for exactly that
  batch's confirmed-cloned repos. This is what step 0's `unsynced` check
  reads, so a run that dies mid-batch only leaves *that* batch's repos
  unstamped, not the whole run.

`fetch_marketplace.py` (picking up new repos from the marketplace) is no
longer chained into this step automatically — run it separately as part of
[step 3](#3-update-the-repo-list-always-additive-never-deletes) before
rerunning the pipeline if you want fresh marketplace repos included.

**⚠️ A repo directory that is itself named like a `.md` file** (e.g. a repo
with a literal `soul.md/` directory containing a nested `SKILL.md`) will
match `index_qdrant.py`'s `*.md` glob by name even though it's a directory,
not a file — `index_qdrant.py` skips these with a `[warn] skipping
non-file ...` line rather than crashing (this used to be an unhandled
`IsADirectoryError`). Nothing to do about these; they're just noted in the
run output.

**Legacy: `archived/run_pipeline.sh`.** The original single-shot pipeline
runner (fetch marketplace → clone everything → extract → index →
mark-synced) has been moved to `archived/` — it clones the *entire*
matching set into `repos/` in one shot with no batching, which is exactly
the disk-filling failure mode `batch_pipeline.py` was built to avoid. Kept
around for reference only; don't run it as part of the regular workflow.

## 5. Optional scan publication (`--publish-scans`)

Scan publication is an explicit opt-in stage. It is off by default; add
`--publish-scans` to `batch_pipeline.py` only after the ordinary clone and
index inputs are ready. The publisher operates on the current batch's
`SKILL.md` files and publishes only locations that already exist in the
`agent_skills` Qdrant collection. A skill that is not indexed is skipped —
the publisher never creates a Qdrant collection or indexes a skill as a side
effect. The stage skips unindexed skills. Successful or duplicate publication
receipts are stored on the matching point under
`locations[].vettd_scan_publications`. Failed attempts remain in the
per-run failure summary/log and are retried on a later run; they do not create
a durable receipt.

Publication is best-effort per skill: one failed scan or submit does not stop
the remaining skills or the indexing stage, but any publication failure makes
the final process exit nonzero. Treat a nonzero exit as a partial result and
inspect the per-location receipts before retrying.

**Rescan interval.** A skill is not rescanned on every run just because the
pipeline touched it again — see `docs/ARCHITECTURE_PUBLISHING_SCANS.md` for
the full algorithm. In short: no prior receipt for the configured target
means an unconditional scan (nothing to compare against); otherwise a time
gate applies first (`VETTD_RESCAN_INTERVAL_DAYS`, default **7**) before the
folder's content hash is even computed, and only past that interval does an
unchanged hash actually skip the rescan. Lower it (e.g. `0`) for a
same-run/testing loop where every content change should be caught
immediately; raise it to reduce scan volume on a large, slow-changing
registry.

**Verified so far:** `publish_scans.py` has been run directly (not through
`batch_pipeline.py`) against a real local `vettd` backend and the real
`vettd-cli` binary, over real skills from `search-raw/affaan-m/ECC` and its
rename `affaan-m/everything-claude-code` — first-scan, idempotent rerun, and
all four rescan-gating branches (recent+changed, stale+unchanged,
stale+changed, never-scanned) all behaved as documented, confirmed against
the dev-server's own access log and Postgres, not just this script's exit
code. Two real bugs were found and fixed this way (an `AuthStatus` schema
that rejected real CLI output, and a `ScanReport` type too shallow for a
real scan report) — both were invisible to the unit test suite alone, which
mocks `vettd-cli`'s output. **Not yet verified:** a live run through
`batch_pipeline.py --publish-scans` itself (only unit-tested with mocks),
and any run against a real production `VETTD_API_KEY`/endpoint or at real
registry scale — mint and test both before relying on this in an unattended
daily job.

### LLM threat scan — partially prototyped, not wired into the pipeline

A second scan step: select the top skills by stars that already carry a Vettd
**security** finding, run the non-deterministic LLM threat scan
(`POST /scan` on the FastAPI app), and record each verdict as a top-level
`llm_scan` payload field on the skill's Qdrant point. Target design folds the
Qdrant write into the API (`POST /scan/skill`) so the whole thing is one call.

Status and roadmap: `LLM_SCANNING_PROJECT_PLAN.md`. Design:
`docs/ARCHITECTURE_LLM_SCAN.md`. The selection step (`scan_top_skills.py`) and
the `--with-scan` wiring do **not** exist yet.

**How to run it now** (one skill, end to end, via `smoke_scan_top_skills.py`):

```bash
# Qdrant up + agent_skills indexed
uv run python stats.py

# a FastAPI instance that serves /scan  (the long-running :8000/:8001 servers
# predate the /scan wiring, so start a fresh one)
export OPENROUTER_API_KEY=...          # or SKILL_SCANNER_LLM_API_KEY;
                                       # falls back to ../skill-scan-eval/.env
( cd app && uv run uvicorn query_service:app --host 127.0.0.1 --port 8000 & )

# pick one top skill with a Vettd security finding, scan it via the API,
# write llm_scan to its Qdrant point, read back and assert
uv run python smoke_scan_top_skills.py
```

The smoke test spawns its own throwaway service if none serves `/scan`, so the
`uvicorn` line is optional for the test itself but is what a real run needs. It
makes one real write to `agent_skills` and prints a `delete_payload` one-liner
to undo it.

### Runtime matrix and environment

The Python scripts derive their checkout root from `Path(__file__).parent`.
Shell wrappers should do the same rather than embedding a workstation path:

```bash
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
```

Use the following matrix. `SKILLS_QDRANT_DB_PATH` takes precedence when it is
set, so it must be explicitly unset in every Docker/server invocation.

| Runtime | Checkout root | Qdrant configuration |
|---|---|---|
| Local embedded | `ROOT` derived from the script above | `SKILLS_QDRANT_DB_PATH="$ROOT/qdrant_db"`; `SKILLS_QDRANT_URL` unset |
| Local Docker/server | `ROOT` derived from the script above | `SKILLS_QDRANT_URL=http://localhost:6333`; `SKILLS_QDRANT_DB_PATH` explicitly unset |
| Direct EC2 checkout | e.g. `/home/ec2-user/ah-skills/search-demo` | Docker Qdrant at `SKILLS_QDRANT_URL=http://localhost:6333`; `SKILLS_QDRANT_DB_PATH` explicitly unset |

On every host, set `VETTD_CLI_BIN` to that host's absolute `vettd-cli`
executable. Set `VETTD_SCAN_ENDPOINT` to the complete backend route ending
in `/api/scans/ingest` (not just a hostname). `VETTD_API_KEY` must belong to
the backend account intended to own these scans, and the non-secret
`VETTD_EXPECTED_ACCOUNT_EMAIL` must name that same account.
`VETTD_RESCAN_INTERVAL_DAYS` (default `7`, must be a non-negative integer)
controls the rescan gate described above; leave it unset for the default.
The preflight
compares `vettd auth status --json` fields `configured`, `api_key_set`,
`reachable`, and `account.email` with that expected email. This status check
does **not** prove that the full ingest URL is correct; only a successful
submit acknowledgement (`Scan accepted: ...` or
`Scan already submitted (duplicate).`) proves the ingest route worked. Give
the scanner an isolated,
writable `HOME`; do not reuse a shared home when jobs can overlap. Never put
real keys, private addresses, or host-specific credentials in this document
or `.env.example`.

Local embedded example (the root and all derived paths are local to the
checkout; replace only the placeholders):

```bash
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export HOME="$ROOT/.runtime-home"
export VETTD_CLI_BIN="$ROOT/.local/bin/vettd-cli"  # absolute after ROOT resolves
export VETTD_SCAN_ENDPOINT="http://localhost:3000/api/scans/ingest"
export VETTD_API_KEY="replace-with-vettd-api-key"
export VETTD_EXPECTED_ACCOUNT_EMAIL="dev@localhost"
export SKILLS_QDRANT_DB_PATH="$ROOT/qdrant_db"
unset SKILLS_QDRANT_URL
mkdir -p "$HOME"

uv run python batch_pipeline.py --batch-size 50 --only-unsynced \
  --publish-scans
```

Local Docker/server example:

```bash
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export HOME="$ROOT/.runtime-home"
export VETTD_CLI_BIN="$ROOT/.local/bin/vettd-cli"  # absolute after ROOT resolves
export VETTD_SCAN_ENDPOINT="http://localhost:3000/api/scans/ingest"
export VETTD_API_KEY="replace-with-vettd-api-key"
export VETTD_EXPECTED_ACCOUNT_EMAIL="dev@localhost"
export SKILLS_QDRANT_URL="http://localhost:6333"
unset SKILLS_QDRANT_DB_PATH
mkdir -p "$HOME"

curl --fail --silent --show-error "$SKILLS_QDRANT_URL/healthz" >/dev/null
uv run python batch_pipeline.py --batch-size 50 --only-unsynced \
  --publish-scans
```

Direct EC2/Docker example (this block intentionally has no workstation
absolute paths):

```bash
ROOT="/home/ec2-user/ah-skills/search-demo"
cd "$ROOT"
export HOME="$ROOT/.runtime-home"
export VETTD_CLI_BIN="/home/ec2-user/.local/bin/vettd-cli"  # absolute on EC2
export VETTD_SCAN_ENDPOINT="https://backend.example.invalid/api/scans/ingest"
export VETTD_API_KEY="replace-with-vettd-api-key"
export VETTD_EXPECTED_ACCOUNT_EMAIL="replace-with-production-account-email"
export SKILLS_QDRANT_URL="http://localhost:6333"
unset SKILLS_QDRANT_DB_PATH
mkdir -p "$HOME"

curl --fail --silent --show-error "$SKILLS_QDRANT_URL/healthz" >/dev/null
uv run python batch_pipeline.py --batch-size 50 --only-unsynced \
  --publish-scans
```

### Preflight and health checks

Run these before a publish-enabled job. They fail without printing the API
key, and the Qdrant check works only for server mode; embedded mode instead
checks that the configured path is present and writable:

```bash
set -eu
: "${VETTD_CLI_BIN:?set an absolute VETTD_CLI_BIN for this host}"
: "${VETTD_SCAN_ENDPOINT:?set the full /api/scans/ingest endpoint}"
: "${VETTD_API_KEY:?set the intended backend account API key}"
: "${VETTD_EXPECTED_ACCOUNT_EMAIL:?set the intended backend account email}"
: "${HOME:?set an isolated writable HOME}"
case "$VETTD_SCAN_ENDPOINT" in
  */api/scans/ingest) ;;
  *) echo "VETTD_SCAN_ENDPOINT must end in /api/scans/ingest" >&2; exit 2 ;;
esac
test -x "$VETTD_CLI_BIN"
test -d "$HOME" && test -w "$HOME"
if [ -n "${SKILLS_QDRANT_URL:-}" ]; then
  test -z "${SKILLS_QDRANT_DB_PATH:-}"
  curl --fail --silent --show-error "$SKILLS_QDRANT_URL/healthz" >/dev/null
else
  : "${SKILLS_QDRANT_DB_PATH:?set SKILLS_QDRANT_DB_PATH for embedded mode}"
  test -d "$SKILLS_QDRANT_DB_PATH" && test -w "$SKILLS_QDRANT_DB_PATH"
fi
"$VETTD_CLI_BIN" --version >/dev/null
AUTH_STATUS="$("$VETTD_CLI_BIN" auth status --json)"
printf '%s\n' "$AUTH_STATUS" | jq -e \
  --arg expected "$VETTD_EXPECTED_ACCOUNT_EMAIL" \
  '(.configured == true) and (.api_key_set == true) and
   (.reachable == true) and (.account.email == $expected)' >/dev/null
uv run python batch_pipeline.py --help | grep -F -- '--publish-scans'
```

For a dry configuration parse without contacting EC2 or a backend, copy one
of the blocks above into `env -i` with a known `PATH`, then run the preflight
variable checks while replacing `test -x "$VETTD_CLI_BIN"`, the `curl` line,
and the `vettd auth status --json`/`jq` lines with `printf 'configured\n'`.
Do not copy a local `VETTD_CLI_BIN`, `HOME`, `VETTD_EXPECTED_ACCOUNT_EMAIL`, or
`SKILLS_QDRANT_DB_PATH` into the EC2 block; all four are host-local or
environment-specific values. Keep the real values in an untracked `.env`, and
source it with `set -a; . .env; set +a` before running the checks.

## Non-obvious issues / things that can bite you

- **Registry `skip` does nothing yet (see step 1).** If you're trying to
  reduce clone/disk/index load by skipping noisy repos, it won't — until
  `repo_pairs()` in `registry.py` is updated to filter on `status`, skipped
  repos are still cloned, extracted, and indexed exactly as before.
- **Skipping a repo does not delete anything already on disk.** Even once
  skip filtering is implemented, the design decision (per earlier
  discussion) was to leave already-cloned `repos/<owner>/<repo>` directories
  alone rather than auto-deleting them — so disk usage under `repos/` only
  grows over time regardless of skip status, unless someone manually
  cleans it up.
- **Blacklisting requires a rerun to take effect** (see step 2) — it's not
  a live query-time filter.
- **`qdrant_db/` is a local embedded store, not a server — it does not
  support concurrent writers.** If you (or a cron job) run `index_qdrant.py`
  / `batch_pipeline.py` while another instance is already running, the
  second one crashes with `RuntimeError: Storage folder ... is already
  accessed by another instance of Qdrant client`. Make sure
  `batch_pipeline.py` invocations don't overlap (e.g. a cron job that takes
  longer than its own interval).
- **`fetch_marketplace.py` always clones each plugin's repo default branch**
  (`--depth 1`, via `clone_repos.py`), **not** the specific `ref`/`sha`/
  `commit` a plugin manifest entry pins to. That pinned metadata is kept on
  the registry entry for reference, but the actual clone can drift from
  what a given marketplace plugin version specifies.
- **A handful of marketplace plugins have no dedicated repo** — their
  `source` is a bare local path like `./plugins/x`, meaning the plugin lives
  inside the marketplace repo itself. `fetch_marketplace.py` resolves these
  via the plugin's `homepage` URL when possible, falling back to
  `anthropics/claude-plugins-public` (every case seen so far). If Anthropic
  restructures that repo, or a future plugin has neither a resolvable
  homepage nor lives in that fallback repo, `fetch_marketplace.py` prints a
  `[warn] could not resolve a repo for ...` line — check for that in the
  cron logs occasionally.
- **`extract_search_raw.py` has no general staleness cleanup** — if a
  repo is deleted from GitHub, or a `SKILL.md` is renamed/removed upstream,
  the old copy under `search-raw/` is *not* automatically removed on
  rescan unless it's specifically blacklisted (blacklist removal is the one
  case that's handled). This is a pre-existing gap, not something this
  daily job fixes.
- **Stale local clone state on a new/rebuilt machine.** `repo-seeds/registry.json`
  is tracked in git, so `last_synced` timestamps travel with the repo to a
  fresh checkout even though `repos/`, `search-raw/`, and `.clone_state.json`
  (all gitignored, local-only) do not. Two separate staleness clocks can
  disagree as a result: `registry.unsynced_today()` (step 0) only checks
  whether `last_synced` matches *today's* date, so an old timestamp still
  correctly flags a repo as due for resync — but `clone_repos.py`'s own
  independent `RECLONE_COOLDOWN_SECONDS` (30 days, tracked in
  `.clone_state.json`) will silently skip re-cloning anything cloned within
  the last month *regardless* of `--only-unsynced`, printing `[skip] ...
  cloned within the last 30 days` — even when `repos/`/`search-raw/` are
  actually empty on this machine because they were never carried over from
  wherever `.clone_state.json` was last written. On a new box (or after
  losing local state some other way), back up and clear `.clone_state.json`
  before step 4, or the run will look successful while doing nothing.
