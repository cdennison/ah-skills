# LLM threat-scan step for top skills

> **Status: partially prototyped.** The pure `POST /scan` endpoint is committed
> (`a658323`); an end-to-end smoke test (`smoke_scan_top_skills.py`) proves the
> full loop against the live collection. The selection step
> (`scan_top_skills.py`), the Qdrant-writing endpoint (`POST /scan/skill`), and
> the re-index preservation hook are **not built yet**. Running status and roadmap:
> [`../LLM_SCANNING_PROJECT_PLAN.md`](../LLM_SCANNING_PROJECT_PLAN.md).

## Purpose

Run the non-deterministic LLM threat scan across the highest-value indexed
skills and persist each verdict onto that skill's Qdrant point, so search, CSV
exports, and the Next.js frontend can surface a real security assessment
instead of the current mock `SecurityStatus`.

Manually triggered for now. Designed to be wired into `batch_pipeline.py`
(`--scan-top-skills`) and `RUN.sh` (opt-in `--with-scan` step) later, the same
way `publish_scans.py` is wired via `--publish-scans`.

## Core design decision: scan + Qdrant write are both behind one API call

The pipeline step does **not** touch Qdrant for writes. It selects target
skills and calls the FastAPI service once per skill; the **service** looks up
the point, scans its `SKILL.md` text, applies rescan gating, writes the
`llm_scan` payload field, and returns the verdict. This keeps all
scanner-model config, prompt versioning, structured-output parsing, and the
payload schema in exactly one place, and gives the Next.js app (and anything
else) the same "scan this skill and record it" primitive.

Two endpoints:

| Endpoint | Qdrant | Use |
|---|---|---|
| `POST /scan` | none — pure `text → verdict` | eval harness (`skill-scan-eval/`), ad-hoc scans, reproducibility. **Stays pure.** |
| `POST /scan/skill` *(to build)* | reads the point, writes `llm_scan` | the pipeline step, the frontend, the smoke test |

## How it relates to the two existing scan systems

| | Vettd deterministic scan (`publish_scans.py`) | LLM threat scan (this doc) |
|---|---|---|
| Engine | external `vettd` binary | `POST /scan[/skill]` → litellm/OpenRouter |
| Determinism | deterministic | **non-deterministic** (no temperature/seed) |
| Where results live | `locations[].vettd_scan_publications` (receipts) + `locations[].vettd_scan_findings` (severity/grade/category rollup) | new top-level `llm_scan` payload field (full verdict) |
| Input | the extracted skill *folder* under `search-raw/` | the `SKILL.md` `content` string already on the Qdrant point |
| Selection | every extracted skill in the batch | top-N by stars, filtered to skills that already have a Vettd **security** finding |

The LLM scan **depends on** the Vettd scan as its selection signal.

## Data flow

```
agent_skills (Qdrant)
  │  scan_top_skills.py — select: stars desc, take SCAN_TOP_N,
  │                       keep only points with a Vettd "security" finding
  ▼  (per skill, bounded concurrency)
POST {SCAN_SERVICE_URL}/scan/skill  {point_id, model?, force?}
  │
  ├─ FastAPI: retrieve point → read `content`
  ├─          rescan gate vs existing `llm_scan`
  ├─          scan_skill_text(content)  →  litellm / OpenRouter
  │                                        (Cisco threat-analysis prompt)
  └─          client.set_payload("agent_skills", {"llm_scan": {…}}, [point_id])
  ▼
{point_id, skipped: bool, llm_scan: {…}}
```

`scan_top_skills.py` structure mirrors `publish_scans.py`: a
`Config.from_env()` dataclass, `preflight()` (validate config, open Qdrant for
*reads*, probe the scan service), a `scan_top_skills()` batch driver returning
a summary, and a thin `main(argv)`. Self-contained `uv run` script header like
the other root scripts.

## Selecting skills (`scan_top_skills.py`)

`select_skills(client, config) -> list[str]` (point ids):

1. Scroll `agent_skills` with
   `with_payload=["name","stars","locations","agent_compatibility","llm_scan"]`,
   `with_vectors=False`. A full client-side scroll is the norm in this repo —
   `agent_skills` has no payload index on nested `locations[]` fields, so a
   server-side `scroll_filter` on them times out at collection scale (see
   `TEST_PLAN_FINDINGS_SUMMARY_TOP1000.md` §5).
2. Sort by `stars` descending; tie-break on point id so the pick never depends
   on scroll order. Take the first `SCAN_TOP_N` (default 100).
3. **Filter — security signal is the Vettd deterministic scan.**
   Keep a point when any `locations[]` entry has
   `vettd_scan_findings.categories_flagged` containing `"security"`.
   `--all` / `SCAN_REQUIRE_SECURITY_SIGNAL=0` disables the filter.
4. **Exclude openclaw / hermes.** Drop any point whose `agent_compatibility`,
   `name`, or any location `path` / `owner` contains `openclaw` or `hermes`
   (case-insensitive) — the substring check is needed because
   `agent_compatibility` is empty for ~81% of points on the current index
   (degraded classifier, see `TEST_PLAN_FINDINGS_SUMMARY_TOP1000.md` §5c).

Selection is deliberately isolated in one small function — the criteria are
expected to change (gate on real Vettd *severity* once persisted; widen beyond
stars; revisit the agent exclusions). Do **not** reuse
`export_csv.py --ranked-only` — that orders by `best_rank` (skills.sh install
rank + GitHub search rank), not stars.

## The `/scan/skill` endpoint (to build)

Request: `{ "point_id": "...", "model": null, "force": false }`
(accept `content_hash` as an alternative to `point_id`).

1. `client.retrieve("agent_skills", ids=[point_id], with_payload=["content","name","content_hash","llm_scan"])`.
   404 if absent.
2. **Rescan gate** (mirrors `publish_scans.py`'s algorithm): skip — return
   `{skipped: true, llm_scan: <existing>}` — when the existing `llm_scan` has
   `content_sha256` == sha256 of the point's current `content`, **and**
   `now - scanned_at` < `SCAN_RESCAN_INTERVAL_DAYS` (default 7), **and**
   `model` + `prompt_version` unchanged. `force: true` bypasses. Never skip on
   age alone — the scan is non-deterministic.
3. `scan_skill_text(content, name, model)` — reuse the existing function.
4. Build the `llm_scan` object (below) and
   `client.set_payload("agent_skills", {"llm_scan": {…}}, points=[point_id])`.
5. Return `{point_id, skipped: false, llm_scan}`.

This makes the FastAPI service a **Qdrant writer** for the first time
(`query_service.py` is read-only today). Scope the write to the single
`llm_scan` key via `set_payload`; the service still never creates a collection
or upserts vectors.

### The `llm_scan` payload field

One verdict per point (per unique content), **latest only — no history**:

```json
{
  "llm_scan": {
    "model": "openrouter/deepseek/deepseek-v3.2",
    "prompt_version": "37243f9d5700",
    "scanned_at": "2026-08-29T15:26:13+00:00",
    "content_sha256": "<sha256 of the SKILL.md text scanned>",
    "max_severity": "HIGH",
    "finding_count": 3,
    "primary_threats": ["Indirect Prompt Injection"],
    "overall_assessment": "…",
    "findings": [ /* ScanFinding objects verbatim from the scan */ ]
  }
}
```

`prompt_version` = `sha256(prompt_file)[:12]`. `max_severity` = max over
`findings[].severity` (`CRITICAL` > `HIGH` > `MEDIUM` > `LOW`), or `"NONE"`.

## Preserving `llm_scan` across re-index

`index_qdrant.py`'s `upload_in_batches()` rebuilds a fresh `PointStruct` per
skill and `upsert`s it, replacing the whole payload.
`_preserve_scan_publications()` already carries `vettd_scan_publications` /
`vettd_scan_findings` forward — extend it to also copy a stored top-level
`llm_scan` onto the incoming `SkillPayload` when the point id matches (same
`client.retrieve` call; add `"llm_scan"` to its `with_payload` list). The point
id is `point_id(content_hash)`, so a preserved `llm_scan` always matches the
same `SKILL.md` content. `--refresh-metadata` (`set_payload` / `delete_payload`)
already leaves unlisted fields untouched.

## FastAPI service changes

### `POST /scan` — keep pure, one addition

Return `prompt_version` (`sha256(PROMPT_PATH.read_bytes())[:12]`) on
`ScanResponse` so callers can record what produced a verdict. Otherwise
unchanged: text in → verdict out, no Qdrant.

### `POST /scan/skill` — new (see above)

### `POST /query` — expose `llm_scan` for the Next.js app

The Next.js frontend consumes the `/query` response (`SkillHit` in
`app/query_service.py`; see `NEXTJS_INTEGRATION.md`, `QUERY_INTERFACE.md`).
Once `llm_scan` is on the payload:

- Add `llm_scan_max_severity`, `llm_scan_finding_count`, `llm_scan_scanned_at`
  to `SkillHit` (optionally the full findings list behind a flag).
- Populate them in `app/search.py` (`SkillPayload` / `SearchResult` / row
  mapping, plus `browse_skills()`).
- Regenerate `app/openapi.json`; update `NEXTJS_INTEGRATION.md` /
  `QUERY_INTERFACE.md`.
- Optionally retire `pick_random_security_status()` for the real value.

## Running this now

Nothing is wired into `RUN.sh` yet. The manual loop, proven by
`smoke_scan_top_skills.py`:

```bash
# 1. Qdrant up, agent_skills indexed
curl -s localhost:6333/collections | grep agent_skills
uv run python stats.py

# 2. a FastAPI instance that serves /scan  (the long-running :8000 / :8001
#    servers predate the /scan wiring — start a fresh one)
export OPENROUTER_API_KEY=...            # or SKILL_SCANNER_LLM_API_KEY
cd app && uv run uvicorn query_service:app --host 127.0.0.1 --port 8000

# 3. end-to-end smoke test: pick one top skill with a Vettd security finding,
#    scan it via the API, write llm_scan to its Qdrant point, read back + assert
cd .. && uv run python smoke_scan_top_skills.py
```

`smoke_scan_top_skills.py` spawns its own throwaway service if none is
reachable (`SCAN_SERVICE_URL` unset or missing `/scan`), so step 2 is optional
for the smoke test but is what a real run needs. It falls back to
`../skill-scan-eval/.env` for the API key. The test makes one real write to
`agent_skills` and prints a `delete_payload` one-liner to undo it.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SCAN_SERVICE_URL` | no | `http://localhost:8000` | FastAPI base URL |
| `SCAN_TOP_N` | no | `100` | how many top-by-stars skills to consider |
| `SCAN_REQUIRE_SECURITY_SIGNAL` | no | `1` | apply the "has a Vettd security finding" filter |
| `SCAN_RESCAN_INTERVAL_DAYS` | no | `7` | rescan gate; mirrors `VETTD_RESCAN_INTERVAL_DAYS` |
| `SCAN_MODEL` | no | — | litellm model id passed through to the scan |
| `SKILLS_QDRANT_URL` / `SKILLS_QDRANT_DB_PATH` | one, not both | — | Qdrant selection; the service now needs write access |
| `OPENROUTER_API_KEY` / `SKILL_SCANNER_LLM_API_KEY` (on the *service*) | yes | — | consumed by the scan endpoint |

## Wiring into the larger pipeline (later)

- **`batch_pipeline.py`:** `--scan-top-skills` next to `--publish-scans`. Reads
  the *indexed* collection, so run it **once after the final index**, not per
  batch.
- **`RUN.sh`:** opt-in `[8.5/9]` step between index (8) and CSV export (9),
  gated behind `--with-scan` + a `SCAN_SERVICE_URL` reachability check,
  default skipped — same treatment as `--with-leaderboard` / `--with-search`.
- **`export_csv.py`:** `llm_scan_max_severity` / `llm_scan_finding_count`
  columns.

## Smoke tests

- **`smoke_scan_top_skills.py`** (exists) — the full loop on one real skill:
  Qdrant query → `/scan` API → write `llm_scan` → read back + assert.
- **When `/scan/skill` lands**, retarget the smoke test at it: `POST
  /scan/skill {point_id}` → assert the endpoint (not the test) wrote a
  well-formed `llm_scan`, then re-POST and assert `skipped: true`.

## Out of scope

- **MCP servers** — no LLM scan of the `mcp_servers` collection yet.
- **Verdict history** — latest `llm_scan` only.
- Selecting on real Vettd severity (needs `publish_scans.py` to persist it —
  now partly done via `vettd_scan_findings.severity_counts`).

## Verification (for the full step, when implemented)

1. Service up with `/scan/skill`; Qdrant up, `agent_skills` populated.
2. `uv run python scan_top_skills.py --dry-run --top-n 10` — prints the
   selection (stars desc, security-filtered, no openclaw/hermes).
3. `uv run python scan_top_skills.py --top-n 5` →
   `attempted=5 scanned=5 skipped=0 failed=0`.
4. Retrieve those ids `with_payload=["llm_scan"]` — verdict present, complete.
5. Re-run step 3 → `skipped=5`; with `--force` → `scanned=5`.
6. Re-run `index_qdrant.py` (or targeted `--ids`), re-retrieve — `llm_scan`
   preserved.
7. `curl :8000/query -d '{"query":"","asset_type":"skill"}'` — hits carry
   `llm_scan_max_severity` / `llm_scan_finding_count`.
8. `uv run pytest test_scan_top_skills.py test_index_qdrant_publications.py`.
