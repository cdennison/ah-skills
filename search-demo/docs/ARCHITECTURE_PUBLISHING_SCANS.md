# Publishing scans to Vettd

`publish_scans.py` scans indexed skill directories with the `vettd` CLI and
submits the results to a Vettd backend. It runs standalone (`uv run
publish_scans.py DIR [DIR ...]`) or as an opt-in step inside
`batch_pipeline.py` (`--publish-scans`), after extraction and (normally)
after that batch's `index_qdrant.py` pass.

Scan state lives entirely inside the Qdrant point payload for each skill's
`locations` entry, as a `vettd_scan_publications` list of receipts — there is
no separate registry file for this. Each receipt records:

```json
{
  "target_fingerprint": "sha256(endpoint \0 api_key)",
  "endpoint": "https://.../api/scans/ingest",
  "content_sha256": "<folder hash>",
  "scanner_version": "1.2.3",
  "scan_id": "...",
  "status": "accepted" | "duplicate",
  "published_at": "2026-08-21T12:00:00+00:00"
}
```

`target_fingerprint` identifies *which backend + credential* a scan was
published to (hashed, so the payload never stores the raw key) — a skill can
carry receipts for multiple targets (e.g. local dev vs. production) side by
side.

## Rescan algorithm

For each skill directory, decide whether to scan and publish using this
order — each step is a short-circuit, cheapest checks first:

1. **Locate the skill's scan history.** Find the skill's Qdrant point via its
   `locations` entry, and look at its `vettd_scan_publications` receipts for
   the current target (matching `target_fingerprint`). If any exist, take
   the most recent one's `published_at`.

2. **No prior receipt for this target → always rescan.** If there is no
   scan history at all for this skill against this target, skip straight to
   scanning — there's nothing to compare a hash against, so the absence of
   any result is itself the signal that a scan is needed. Do not compute the
   folder hash in this case.

3. **Time gate.** If a receipt exists, compare `now - published_at` against
   the configured rescan interval (`VETTD_RESCAN_INTERVAL_DAYS`, default
   **7 days**). If less than the interval has elapsed, skip — do not scan,
   and do not bother hashing the folder. This is a pure time check with no
   content inspection.

4. **Content check, only past the time gate.** Once the interval has
   elapsed, compute the folder's content hash and compare it to the most
   recent receipt's `content_sha256`:
   - Hash unchanged → skip (nothing to rescan; the skill hasn't changed
     since it was last scanned, only time has passed).
   - Hash changed → rescan and publish a new receipt.

The time gate (step 3) exists to bound how often a *previously-scanned,
unchanged* skill gets rescanned — 7 days is a ceiling on staleness, not a
floor. Step 2 exists so a skill that has never been scanned isn't stuck
waiting for a time window that never started.

## Config

All configuration comes from environment variables — no secrets or
endpoints are hardcoded in the script:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `VETTD_CLI_BIN` | no | `vettd` (PATH lookup) | Path to the `vettd` binary |
| `VETTD_SCAN_ENDPOINT` | yes | — | Must be the exact ingest URL, e.g. `https://host/api/scans/ingest` — no query string, fragment, or embedded credentials |
| `VETTD_API_KEY` | yes | — | Never logged; redacted from every error message and command echo |
| `VETTD_EXPECTED_ACCOUNT_EMAIL` | yes | — | Verified against `vettd auth status --json`'s account email during preflight, so a stray/incorrect key is caught before any scan runs |
| `VETTD_RESCAN_INTERVAL_DAYS` | no | `7` | Minimum time between scans of an unchanged skill (see algorithm above) |
| `SKILLS_QDRANT_DB_PATH` / `SKILLS_QDRANT_URL` | one, not both | — | Same embedded-vs-server Qdrant selection as `search.py` |

## Preflight

Before any skill is touched, `preflight()`:

1. Runs `vettd --version` to capture the scanner version (recorded in every
   receipt, so a receipt's hash-match is also implicitly scoped to the
   scanner version that produced it).
2. Runs `vettd auth --key ... --endpoint ...` to point the CLI at the
   target backend.
3. Runs `vettd auth status --json` and verifies both the endpoint and the
   account email match what was configured — `reachable: true` alone does
   **not** prove the endpoint is wired correctly (a different internal probe
   derives its own path), so this checks the full auth status shape instead.
4. Opens Qdrant and confirms the `agent_skills` collection exists.

Any preflight failure aborts before scanning starts (exit code 2 from
`batch_pipeline.py`; nothing is cloned or extracted).

## Failure handling

A per-skill scan or submit failure (`PublishSkillError`) is caught and
recorded in the batch's `PublishSummary.failures` — it does not stop the
rest of the batch, or later batches, from being attempted. `batch_pipeline.py`
exits 1 at the end of the whole run if any skill failed across any batch,
after every batch has had its chance to clone/extract/index/publish.

A repo whose skills failed to publish is **not** marked synced
(`mark_synced_pairs` is skipped for that batch), so it's retried on the next
pipeline run.
