# First-time test: scan publication on EC2, small batch (~100 repos)

Goal: run the daily job's `--publish-scans` stage for the first time on a
real EC2 host, against a real (not local-dev) `vettd` backend, bounded to
about 100 repos — enough to see it actually work end to end without
committing to a full registry-scale run (1,553 repos as of this writing)
on the first try.

This assumes `DAILY_JOB.md` step 5 (`## 5. Optional scan publication`) and
`docs/ARCHITECTURE_PUBLISHING_SCANS.md` (the rescan algorithm) — read both
first if you haven't. This doc is the "first time, small, and verified"
companion to those, not a replacement.

---

## 0. Before you do anything: know which backend you're pointing at

This is the one mistake that's hard to undo cheaply — publishing 100 real
scans to the *wrong* backend/account. Before touching EC2:

- Confirm `VETTD_SCAN_ENDPOINT` is the environment you actually intend
  (staging vs. production) — it must be the full path ending in
  `/api/scans/ingest`, per `DAILY_JOB.md`.
- Confirm `VETTD_API_KEY` belongs to the account you intend to own these
  scans, and `VETTD_EXPECTED_ACCOUNT_EMAIL` names that same account —
  `publish_scans.py`'s preflight verifies this itself and refuses to start
  if they don't match, but only if you set `VETTD_EXPECTED_ACCOUNT_EMAIL`
  correctly in the first place.
- If a staging/test backend and account exist, use that for this first run
  instead of production, even if the end goal is production. Nothing below
  requires production to validate the mechanism.

## 1. EC2 prerequisites (one-time)

Per `DAILY_JOB.md`'s runtime matrix ("Direct EC2 checkout" row):

```bash
ROOT="/home/ec2-user/ah-skills/search-demo"   # adjust to actual checkout path
cd "$ROOT"
uv sync --all-groups
```

Confirm `vettd-cli` is present on this host (built or deployed separately —
this repo never builds it):

```bash
"$VETTD_CLI_BIN" --version
```

Confirm Qdrant is reachable (Docker/server mode is the expected EC2 setup,
not embedded — see the runtime matrix):

```bash
curl --fail --silent --show-error "$SKILLS_QDRANT_URL/healthz"
```

Confirm the `agent_skills` collection already exists and has points — this
stage never creates the collection, only publishes scans for skills already
indexed by a prior `index_qdrant.py` run:

```bash
curl -s "$SKILLS_QDRANT_URL/collections/agent_skills" | python3 -m json.tool | grep -E '"points_count"|"status"'
```

## 2. Set environment (isolated `HOME`, real credentials)

```bash
export HOME="$ROOT/.runtime-home"
mkdir -p "$HOME"
export VETTD_CLI_BIN="/home/ec2-user/.local/bin/vettd-cli"   # absolute path on this host
export VETTD_SCAN_ENDPOINT="https://<your-backend>/api/scans/ingest"
export VETTD_API_KEY="<minted for the intended account>"
export VETTD_EXPECTED_ACCOUNT_EMAIL="<that account's email>"
export SKILLS_QDRANT_URL="http://localhost:6333"
unset SKILLS_QDRANT_DB_PATH
# Leave VETTD_RESCAN_INTERVAL_DAYS unset for the default (7 days) -- this
# is a first-time run against never-scanned skills, so the interval won't
# matter yet (see step 5), but don't override it just for this test.
```

Run `DAILY_JOB.md`'s preflight/health-check block now, in full, before
anything else — it fails fast (missing binary, wrong endpoint shape, auth
mismatch) without touching a single repo.

## 3. Run exactly one bounded batch of 100 repos

`batch_pipeline.py` has no "stop after N repos total" flag — `--batch-size`
only controls how many repos are in *each* batch; left running, it keeps
going until the whole registry is covered. To get a hard, safe stop at
~100 repos on this first run:

```bash
uv run python batch_pipeline.py --batch-size 100 --publish-scans
```

**Watch the output. Stop it (Ctrl+C) after you see this exact sequence for
batch 1, and before `=== Batch 2` appears:**

```
=== Batch 1: 100 repos ([0:100) of 1553) ===
[run] ... clone_repos.py ...
[run] ... extract_search_raw.py ...
[run] ... index_qdrant.py ...
[publish-scans] batch 1: attempted=... succeeded=... skipped=... failed=...
[mark-synced] .../100 repos in this batch
[run] bash .../clean_repos.sh
```

This is a **safe** stopping point, not an interrupted-mid-write one — traced
directly from `batch_pipeline.py`'s loop: each batch's clone → extract →
index → publish → mark-synced → `clean_repos.sh` fully completes before the
loop ever starts batch 2's clone. Nothing is left half-applied if you
Ctrl+C right after `clean_repos.sh` finishes and before the next `===
Batch` line. If you're unsure whether it's safe yet, wait for that cleanup
line to actually print rather than guessing from elapsed time.

(If you'd rather not rely on timing a Ctrl+C, run the four steps by hand
instead — this is exactly what one batch does internally, and gives you an
unambiguous stop between each:

```bash
uv run python clone_repos.py --offset 0 100
uv run python extract_search_raw.py
uv run python index_qdrant.py
# publish_scans.py needs actual skill directories, not repo pairs -- list
# them the same way batch_pipeline.py does:
python3 -c "
from pathlib import Path
root = Path('.')
skill_dirs = []
seen = set()
for repo_dir in sorted((root / 'search-raw').glob('*/*')):
    for skill_path in sorted(repo_dir.rglob('SKILL.md')):
        if skill_path.parent not in seen:
            seen.add(skill_path.parent)
            skill_dirs.append(skill_path.parent)
print('\n'.join(str(d) for d in skill_dirs))
" > /tmp/first-batch-skill-dirs.txt
wc -l /tmp/first-batch-skill-dirs.txt
uv run publish_scans.py $(cat /tmp/first-batch-skill-dirs.txt)
bash clean_repos.sh
```

this variant scans *every* skill under all 100 cloned repos, not just the
ones `batch_pipeline.py` would scope to that batch's confirmed-cloned
pairs — close enough for a first smoke test, but don't treat the count as
identical to what `--publish-scans` would report.)

## 4. Verify it actually worked — three independent checks

Don't trust the printed summary line alone (same principle as
`TEST_PLAN_CLAUDE_SCANNING.md` step 4):

**a. `[publish-scans] batch 1: ...` line itself** — `attempted` should be
close to (not necessarily equal to — some repos have zero `SKILL.md`, some
skills may already be blacklisted) the number of `SKILL.md` files across
100 repos. `failed` should be `0` or small; if nonzero, read the
`[publish-scans] batch 1: failed <path>: <message>` lines on stderr before
proceeding to a larger run — do not just note the count and move on.

**b. Qdrant receipts exist for a sample of scanned skills:**

```bash
python3 -c "
import os
from qdrant_client import QdrantClient
client = QdrantClient(url=os.environ['SKILLS_QDRANT_URL'])
points, _ = client.scroll('agent_skills', with_payload=['locations'], limit=20)
found = 0
for p in points:
    for loc in (p.payload or {}).get('locations', []):
        if 'vettd_scan_publications' in loc:
            found += 1
            print(loc['path'], loc['vettd_scan_publications'][-1]['status'], loc['vettd_scan_publications'][-1]['published_at'])
print('receipts found in this sample of 20 points:', found)
"
```

**c. The backend itself reflects the new scans** — hit whatever
authenticated read endpoint the target backend exposes (e.g.
`GET /api/skills`, same pattern as `TEST_PLAN_CLAUDE_SCANNING.md` step 4c
and `../vettd-e2e/TEST_PLAN_SCAN_PUBLISH.md` step 9) logged in as the
account `VETTD_API_KEY` belongs to, and confirm the count of newly-visible
skills is in the right ballpark and `userId` matches that account — not
just that *a* row exists somewhere.

## 5. What "success" looks like, and what's normal noise

- `succeeded` + `skipped` should account for essentially all of `attempted`
  (small `skipped` on a *first* run is fine — it means a skill directory
  had no matching Qdrant `locations` entry yet, e.g. `index_qdrant.py`
  hadn't caught up, not a rescan-interval skip, since nothing has a prior
  receipt yet on a true first run).
- Don't expect ~100 repos to produce ~100 scans — one repo can hold zero to
  dozens of `SKILL.md` files (see `docs/ARCHITECTURE.md` for the
  repo-to-skill relationship), and content-identical skills across
  differently-named repos collapse to fewer distinct scans on the backend
  side even though each location still gets its own receipt (see the
  `Skill` `(userId, name)` uniqueness note from the local test plan).
- A nonzero `failed` count on a small first run is worth reading closely,
  not shrugging off — it's cheap to investigate 100 repos' worth of
  failures now versus 1,553 repos' worth later.

## 6. If something looks wrong

- **Preflight fails immediately** (before any repo touched) — nothing was
  published; safe to fix config and rerun from step 3 with no cleanup
  needed.
- **A skill's scan/submit fails mid-batch** — that repo is left unsynced
  (not marked `last_synced`), so it's naturally retried on the next run;
  no manual cleanup needed for it specifically.
- **You published to the wrong backend/account** (step 0's failure mode) —
  this isn't something `publish_scans.py` can undo; it already happened on
  the backend. Coordinate with whoever owns that backend to remove the
  erroneous rows rather than trying to fix it from this side. The local
  Qdrant receipts *can* be cleared if needed (they're just payload data:
  `set_payload` with `vettd_scan_publications` removed from the affected
  `locations` entries), but clearing them alone does not undo what already
  landed on the backend.
- **Version-skew errors** (a schema validation error from `publish_scans.py`
  itself, e.g. `AuthStatus`/`ScanReport` rejecting real CLI output) — this
  bit us once already during local testing (two real bugs, both fixed; see
  `DAILY_JOB.md` step 5's "Verified so far" note). If EC2 runs a different
  `vettd-cli`/backend version than what was tested locally, a similar
  schema mismatch is possible — read the pydantic validation error itself,
  it names the exact field that didn't match.

## 7. Once this looks right

Re-run without a manual stop for the full registry, or keep using
`--start-offset` to resume from where step 3 left off (`--only-unsynced` is
usually simpler for this — it naturally picks up wherever `last_synced`
left off, no offset math required):

```bash
uv run python batch_pipeline.py --batch-size 100 --only-unsynced --publish-scans
```

This is the point where it becomes reasonable to consider wiring
`--publish-scans` into a real recurring cron invocation — not before a
small run like this one has actually been checked against the real
backend.
