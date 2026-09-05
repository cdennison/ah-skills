# Test plan: search-demo scan-publish step (publish_scans.py)

Goal: exercise `publish_scans.py` end to end against real skill folders from
this repo's `search-raw/`, fully local, and confirm scans actually happen,
actually get recorded, and are actually skipped/rescanned per the rescan
algorithm — not just that the script exits 0.

```
publish_scans.py (this repo)
   → vettd-cli `scan folder`      (real scanner, real skill dir)
      → JSON report on disk
         → vettd-cli `scan submit`
            → HTTP POST → vettd backend (local dev server)
                 → receipt written back into the skill's Qdrant
                   `locations[].vettd_scan_publications`
```

This plan only touches `search-demo` and a local Qdrant instance/backend —
it does not modify `vettd` or `vettd-cli` source, per the standing
instruction not to touch those repos from here. See
`docs/ARCHITECTURE_PUBLISHING_SCANS.md` for the full rescan algorithm this
plan is verifying.

Repos/paths involved:

- this repo (`search-demo`) — `publish_scans.py`, `search-raw/`,
  `test-data/ECC/`
- `../vettd-cli` (built binary, not modified)
- `../vettd` (Next.js dev server, not modified) — see
  `../../vettd-e2e/docs/runs/2026-08-13-scan-publish-local.md` for how to bring it up locally;
  don't duplicate that setup here, just reference it

---

## 0. Prerequisites

```bash
cd /Users/c/code/vettd/ah-skills/search-demo
uv sync --all-groups
```

Confirm the vettd dev server is up (see `../../vettd-e2e/docs/runs/2026-08-13-scan-publish-local.md`
steps 1–3 for standing it up and minting an API key against the
`dev@localhost` user):

```bash
curl -s http://localhost:3000/api/health   # → {"status":"ok"}
```

Build `vettd-cli` if not already built:

```bash
cd /Users/c/code/vettd/vettd-cli && cargo build -p vettd-cli --bin vettd
```

---

## 1. Stand up a local Qdrant with a handful of real, known skills indexed

Use a scratch on-disk Qdrant (not the shared dev index) so this plan can't
corrupt real data:

```bash
cd /Users/c/code/vettd/ah-skills/search-demo
export SKILLS_QDRANT_DB_PATH=/tmp/vettd-scan-test-qdrant
rm -rf "$SKILLS_QDRANT_DB_PATH"
```

Index a small, known slice — **use the ECC test data on purpose**: it's the
repo `docs/ARCHITECTURE_PUBLISHING_SCANS.md` and `aggregator_filter.py`
both call out by name (crawled under two names, `ECC` and its rename
`everything-claude-code`, historically a false-positive source for
duplicate detection), so it's a good stress case for "does the right
`locations` entry get found and scanned" rather than a synthetic fixture
that can't reproduce that:

```bash
uv run index_qdrant.py --owner affaan-m --limit 25
```

**What to check:** the command finishes without error, and the collection
has points:

```bash
uv run python3 -c "
from qdrant_client import QdrantClient
c = QdrantClient(path='/tmp/vettd-scan-test-qdrant')
print(c.count('agent_skills', exact=True))
"
```

---

## 2. Pick real skill folders to scan

```bash
find search-raw/affaan-m/ECC -iname "SKILL.md" | head -5
find search-raw/affaan-m/everything-claude-code -iname "SKILL.md" | head -5
```

Pick 2–3 concrete directories spanning both the original and renamed copy,
e.g.:

```
search-raw/affaan-m/ECC/skills/bun-runtime
search-raw/affaan-m/everything-claude-code/skills/bun-runtime
```

(same skill name, two different owner paths — confirms `_find_location`
matches by exact relative path, not by name, and doesn't cross-contaminate
receipts between the two copies).

---

## 3. Configure publish_scans.py and run it against those folders

```bash
export VETTD_CLI_BIN=/Users/c/code/vettd/vettd-cli/target/debug/vettd
export VETTD_SCAN_ENDPOINT=http://localhost:3000/api/scans/ingest
export VETTD_API_KEY="<key minted per ../../vettd-e2e/docs/runs/2026-08-13-scan-publish-local.md step 3>"
export VETTD_EXPECTED_ACCOUNT_EMAIL=dev@localhost
export HOME=/tmp/vettd-e2e-scan-publish-home   # isolated CLI config dir

uv run publish_scans.py \
  search-raw/affaan-m/ECC/skills/bun-runtime \
  search-raw/affaan-m/everything-claude-code/skills/bun-runtime
```

**What to check:**

- Exit code 0.
- Printed summary: `attempted=2 succeeded=2 skipped=0 failed=0`.
- No API key or secret appears anywhere in stdout/stderr (grep for the raw
  key value — should find nothing).

---

## 4. Confirm the scan actually happened and was actually recorded

Three independent checks — don't trust the exit code alone:

**a. Qdrant payload carries a receipt for each location:**

```bash
uv run python3 -c "
from qdrant_client import QdrantClient
c = QdrantClient(path='/tmp/vettd-scan-test-qdrant')
points, _ = c.scroll('agent_skills', with_payload=['locations'], limit=1000)
for p in points:
    for loc in (p.payload or {}).get('locations', []):
        if 'bun-runtime' in loc.get('path', '') and 'vettd_scan_publications' in loc:
            print(loc['path'], loc['vettd_scan_publications'])
"
```

Each printed receipt must have `status` in `{"accepted", "duplicate"}`, a
real `scan_id` (not empty/placeholder), a `content_sha256` that isn't all
zeros, and a `published_at` timestamp from *this run* (check it's within
the last few minutes, not stale from a previous test).

**b. The vettd dev server logged a real ingest, not a silent no-op** — tail
its stdout for:

```
POST /api/scans/ingest 201 in ...ms
```

for each submitted skill (two `201`s expected here) — not `POST / 200`
(the silent-misconfiguration failure mode documented in
`../../vettd-e2e/docs/runs/2026-08-13-scan-publish-local.md`'s deviations section).

**c. The scan is visible through the backend's own read API**, logged in as
the same user the key was minted against:

```bash
COOKIE_JAR=/tmp/vettd-scan-test-cookies.txt
curl -s -c "$COOKIE_JAR" "http://localhost:3000/api/dev/login?secret=dev-local" -o /dev/null
curl -s -b "$COOKIE_JAR" "http://localhost:3000/api/skills?limit=200" | python3 -m json.tool \
  | grep -A5 "bun-runtime"
```

Confirm two distinct entries (one per owner path scanned), each with
`sourceType: "cli"`, non-zero `_count.findings`, and `userId` equal to the
id the API key was minted against.

---

## 5. Confirm idempotency: rerun without any change is a no-op

```bash
uv run publish_scans.py \
  search-raw/affaan-m/ECC/skills/bun-runtime \
  search-raw/affaan-m/everything-claude-code/skills/bun-runtime
```

**What to check:** `attempted=2 succeeded=0 skipped=2 failed=0` — no new
`vettd scan folder`/`scan submit` subprocess actually ran (confirm no new
`POST /api/scans/ingest` line appears in the dev-server log since step 4),
and the receipt(s) in Qdrant are unchanged (same `scan_id`,
`published_at`).

---

## 6. Confirm the rescan-interval time gate

This is the part that's easy to get wrong silently — verify it actually
gates on time, not just on hash.

**a. Within the interval, a changed file still does not trigger a rescan:**

```bash
echo "# touched $(date)" >> search-raw/affaan-m/ECC/skills/bun-runtime/SKILL.md
uv run publish_scans.py search-raw/affaan-m/ECC/skills/bun-runtime
git checkout -- search-raw/affaan-m/ECC/skills/bun-runtime/SKILL.md   # revert the test edit
```

**What to check:** still `skipped=1` — the last scan was seconds ago, well
inside the default 7-day window, so the time gate should short-circuit
*before* the hash is even computed. (If this instead rescans, the time
gate isn't wired ahead of the hash check — a real bug, not expected
behavior.)

**b. Past the interval, an unchanged file is still skipped (hash check
fires, not just time):**

Backdate the existing receipt's `published_at` directly in Qdrant to
simulate 8 days elapsed, then rerun with an unmodified folder:

```bash
uv run python3 -c "
from datetime import datetime, timedelta, UTC
from qdrant_client import QdrantClient
c = QdrantClient(path='/tmp/vettd-scan-test-qdrant')
points, _ = c.scroll('agent_skills', with_payload=['locations'], limit=1000)
for p in points:
    locs = (p.payload or {}).get('locations', [])
    changed = False
    for loc in locs:
        if 'bun-runtime' in loc.get('path', '') and 'ECC/skills' in loc.get('path', ''):
            for receipt in loc.get('vettd_scan_publications', []):
                receipt['published_at'] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
                changed = True
    if changed:
        c.set_payload('agent_skills', {'locations': locs}, points=[p.id])
"
uv run publish_scans.py search-raw/affaan-m/ECC/skills/bun-runtime
```

**What to check:** `skipped=1` still, and no new `scan folder`/`scan submit`
subprocess ran (folder content is unchanged, so past the time gate the hash
check should still find a match and skip) — the receipt's `published_at`
should remain the backdated 8-day-old value, **not** be bumped to now.

**c. Past the interval, a changed file rescans:**

```bash
echo "# touched $(date)" >> search-raw/affaan-m/ECC/skills/bun-runtime/SKILL.md
uv run publish_scans.py search-raw/affaan-m/ECC/skills/bun-runtime
git checkout -- search-raw/affaan-m/ECC/skills/bun-runtime/SKILL.md
```

**What to check:** `succeeded=1` this time — past the time gate, the
changed hash should trigger a real rescan. Confirm a new `scan_id` and a
fresh `published_at` in the receipt, and a new `POST /api/scans/ingest 201`
in the dev-server log.

**d. A skill with no prior receipt at all always scans, regardless of age**
— covered implicitly by step 3/4 (first run, no receipt existed yet), but
worth re-confirming isn't accidentally time-gated: pick a third, never-
before-scanned skill directory and run `publish_scans.py` against it alone.
It must scan immediately (`succeeded=1`), never `skipped`, since there's no
receipt to check age against.

---

## 7. Confirm the case-variant SKILL.md guard doesn't false-positive on ECC

Already checked statically (zero directories under `search-raw/` or
`test-data/ECC` have multiple case-variant `SKILL.md` files — see prior
session notes), but confirm dynamically too: running `publish_scans.py`
against every ECC skill directory should never raise
`"multiple case-insensitive SKILL.md files"`:

```bash
find search-raw/affaan-m/ECC search-raw/affaan-m/everything-claude-code \
  -iname "SKILL.md" -exec dirname {} \; | sort -u > /tmp/ecc-skill-dirs.txt
wc -l /tmp/ecc-skill-dirs.txt

uv run publish_scans.py $(cat /tmp/ecc-skill-dirs.txt) 2>&1 | tee /tmp/ecc-publish-run.log
grep -c "multiple case-insensitive" /tmp/ecc-publish-run.log   # expect 0
```

**What to check:** the failed count in the final summary is 0 (or, if
nonzero, every failure reason is something other than the case-variant
guard — read each one).

---

## 8. Tear down

```bash
rm -rf /tmp/vettd-scan-test-qdrant /tmp/vettd-e2e-scan-publish-home \
       /tmp/vettd-scan-test-cookies.txt /tmp/ecc-skill-dirs.txt /tmp/ecc-publish-run.log
git checkout -- search-raw   # in case any test edit wasn't cleanly reverted above
# vettd dev server / Postgres rows: leave running / tear down per
# ../../vettd-e2e/docs/runs/2026-08-13-scan-publish-local.md step 10
```

---

## Notes for whoever runs this

- Steps 1–2 deliberately use `affaan-m/ECC` / `affaan-m/everything-claude-code`
  instead of a synthetic fixture because that pairing is the repo's own
  documented edge case for "same skill content, indexed under two different
  owner paths" — it's a better correctness check than a fixture that can't
  reproduce it.
- Step 6 is the part most likely to reveal an incomplete implementation:
  if the rescan logic checks the hash *before* the time gate, or vice versa
  in the wrong order, steps 6a and 6b will both look identical
  (`skipped=1`) even though they're exercising different code paths — the
  distinguishing signal in both is **no new subprocess ran and no dev-server
  log line appeared**, not just the summary line, so don't skip that check.
- If step 4's Qdrant check finds a receipt but step 4c's API check finds
  nothing, that's a real bug worth stopping on (indicates the ingest
  succeeded but persistence/read-back is broken) — don't wave it off as
  "eventually consistent" without checking server logs for an actual error.
