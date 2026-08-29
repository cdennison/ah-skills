# LLM threat-scan step for top skills

> **Status: endpoints built, pipeline step not.** `POST /scan` (pure) and
> `POST /scan/skill` (scan + write `llm_scan` to Qdrant) both exist, `/query`
> exposes `llm_scan`, and `smoke_scan_skill.py` proves the whole loop against
> the live collection. Still to build: the selection step (`scan_top_skills.py`),
> the re-index preservation hook, and the `--with-scan` wiring. Running status
> and roadmap: [`../LLM_SCANNING_PROJECT_PLAN.md`](../LLM_SCANNING_PROJECT_PLAN.md).

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
| `POST /scan/skill` | reads the point, writes `llm_scan` | the pipeline step, the frontend, the smoke test |

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

## The `/scan/skill` endpoint

`app/scan_index.py::scan_and_record`, wired at `POST /scan/skill` in
`app/query_service.py`.

Request: `{ "point_id": "...", "model": null, "force": false }` — or
`{ "content_hash": "..." }` instead of `point_id` (exactly one selector).

1. Look up the point: `client.retrieve(..., ids=[point_id])`, or a
   `content_hash` field-match scroll. `SkillNotFound` → **404**.
2. **Rescan gate** (mirrors `publish_scans.py`'s algorithm): return
   `{skipped: true, reason, llm_scan: <existing>}` when the existing `llm_scan`
   has `content_sha256` == sha256 of the point's current `content`, **and**
   `now - scanned_at` < `SCAN_RESCAN_INTERVAL_DAYS` (default 7), **and**
   `model` + `prompt_version` unchanged. `force: true` bypasses. Never skip on
   age alone — the scan is non-deterministic.
3. `scan_skill_text(content, name, model)` — the same function `POST /scan` uses.
4. Build the `llm_scan` object (below) and
   `client.set_payload("agent_skills", {"llm_scan": {…}}, points=[point_id])`.
5. Return `{point_id, skipped: false, reason: null, llm_scan}`.
   `ScanConfigError` → **503**, `ScanUpstreamError` → **502**.

This is the FastAPI service's **only Qdrant write**. It is scoped to the
single `llm_scan` payload key via `set_payload` — never a vector, never a
collection. `query_service.py`'s module docstring records the exception.

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

### `POST /scan/skill` — see above

### `POST /query` — `llm_scan` is exposed on `SkillHit`

`SkillHit` (in `app/query_service.py`) carries a `llm_scan` field: `null`
until the skill has been through `/scan/skill`, otherwise the full `LlmScan`
object. It rides through `search.SkillPayload` → `search.SearchResult` →
`_to_skill_hit` alongside `locations` (which already carries the Vettd
`vettd_scan_findings` / `vettd_scan_publications`), so **one `/query` response
carries both scans**. `app/openapi.json` regenerated. Still open: update
`NEXTJS_INTEGRATION.md` / `QUERY_INTERFACE.md` prose, and optionally retire
`pick_random_security_status()` for the real value.

## Running this now

Nothing is wired into `RUN.sh` yet. `smoke_scan_skill.py` runs the whole loop
against one hard-coded skill:

```bash
# 1. Qdrant up, agent_skills indexed
uv run python stats.py

# 2. (optional) a FastAPI instance that serves /scan/skill — the long-running
#    :8000 / :8001 servers predate this wiring. The smoke test spawns its own
#    throwaway service if none is reachable, so this is only needed for a real run:
export OPENROUTER_API_KEY=...            # or SKILL_SCANNER_LLM_API_KEY
( cd app && uv run uvicorn query_service:app --host 127.0.0.1 --port 8000 & )

# 3. scan a skill via POST /scan/skill, then see the verdict + the Vettd scan
#    together in a POST /query response
uv run python smoke_scan_skill.py
```

`smoke_scan_skill.py` falls back to `../skill-scan-eval/.env` for the API key,
makes one real `llm_scan` write to `agent_skills`, and prints a `delete_payload`
one-liner to undo it.

## Worked example — one `/query`, both scans

Real output from `smoke_scan_skill.py` against
`affaan-m/everything-claude-code/skills/homelab-pihole-dns/SKILL.md` (point
`83a8b1b8-…`), a skill with a published Vettd scan (grade B, VTD-0088
"references external URL" — a `security`-category medium finding). Not an
openclaw/hermes skill.

**Scan it** — the endpoint scans *and* records the verdict:

```
$ curl -sS -X POST http://localhost:8000/scan/skill \
    -H 'Content-Type: application/json' \
    -d '{"point_id": "83a8b1b8-d47b-5c42-9578-adee890b6d9f", "force": true}'

{
  "point_id": "83a8b1b8-d47b-5c42-9578-adee890b6d9f",
  "skipped": false,
  "reason": null,
  "llm_scan": {
    "model": "openrouter/deepseek/deepseek-v3.2",
    "prompt_version": "37243f9d5700",
    "scanned_at": "2026-08-29T16:29:06.784348+00:00",
    "content_sha256": "23a801545a4839c4fc04dc287e1122cc3913016a116b2cb5c7720e193edfa6c2",
    "max_severity": "LOW",
    "finding_count": 1,
    "primary_threats": [],
    "overall_assessment": "This skill package ('homelab-pihole-dns') is a legitimate, well-documented guide ...",
    "findings": [
      {"severity": "LOW", "aitech": "AITech-4.3", "title": "Missing allowed-tools Manifest Field",
       "location": "SKILL.md", "description": "...", "remediation": "..."}
    ]
  }
}
```

(`"force": true` bypasses the rescan gate so the demo always shows a fresh
verdict; a plain `{"point_id": "..."}` returns `"skipped": true` with the
existing `llm_scan` when it is still fresh. The scan is non-deterministic —
`max_severity` / `finding_count` vary run to run.)

**Query it back** — the hit carries `llm_scan` *and* the Vettd scan (inside
`locations[]`):

```
$ curl -sS -X POST http://localhost:8000/query \
    -H 'Content-Type: application/json' \
    -d '{"query": "homelab pihole dns", "asset_type": "skill", "limit": 25}'

{
  "index_ready": true, "query": "homelab pihole dns", "asset_type": "skill",
  "hits": [
    {
      "name": "homelab-pihole-dns",
      "path": "affaan-m/everything-claude-code/skills/homelab-pihole-dns/SKILL.md",
      "stars": 240095,
      "content": "... SKILL.md text ...",
      "llm_scan": {
        "model": "openrouter/deepseek/deepseek-v3.2",
        "prompt_version": "37243f9d5700",
        "max_severity": "LOW", "finding_count": 1,
        "primary_threats": [],
        "findings": [ /* ... */ ]
      },
      "locations": [
        {
          "path": "affaan-m/everything-claude-code/skills/homelab-pihole-dns/SKILL.md",
          "vettd_scan_findings": {
            "scan_id": "6d5fed87-097d-4b08-87c6-a72867058344",
            "overall_grade": "B", "trust_level": "Conditional",
            "has_malicious_findings": false, "finding_count": 13,
            "severity_counts": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 12},
            "categories_flagged": ["security"],
            "top_findings": [
              {"rule_id": "VTD-0088", "category": "security", "severity": "medium",
               "label": "References external URL — review for indirect prompt injection risk"}
            ]
          },
          "vettd_scan_publications": [
            {"scan_id": "6d5fed87-097d-4b08-87c6-a72867058344", "status": "accepted",
             "scanner_version": "0.9.0", "endpoint": "http://localhost:3000/api/scans/ingest",
             "published_at": "2026-08-29T16:26:38.466145+00:00", "...": "..."}
          ]
        }
      ]
    }
  ]
}
```

So a client reads `hit.llm_scan` for the non-deterministic verdict and
`hit.locations[].vettd_scan_findings` / `vettd_scan_publications` for the
deterministic one, from a single query.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SCAN_SERVICE_URL` | no | `http://localhost:8000` | FastAPI base URL |
| `SCAN_TOP_N` | no | `100` | how many top-by-stars skills to consider |
| `SCAN_REQUIRE_SECURITY_SIGNAL` | no | `1` | apply the "has a Vettd security finding" filter |
| `SCAN_RESCAN_INTERVAL_DAYS` | no | `7` | `/scan/skill` rescan gate; mirrors `VETTD_RESCAN_INTERVAL_DAYS` |
| `SCAN_MODEL` | no | — | litellm model id passed through to the scan (step-side) |
| `SKILL_SCANNER_LLM_MODEL` | no | `openrouter/deepseek/deepseek-v3.2` | model the *service* uses when the request sets none |
| `SKILLS_QDRANT_URL` / `SKILLS_QDRANT_DB_PATH` | one, not both | — | Qdrant selection; the service now also **writes** the `llm_scan` key |
| `OPENROUTER_API_KEY` / `SKILL_SCANNER_LLM_API_KEY` (on the *service*) | yes | — | consumed by the scan endpoints |

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

- **`smoke_scan_skill.py`** (repo root) — hard-coded to one real skill with a
  published Vettd scan. Calls `POST /scan/skill`, asserts the *endpoint* wrote
  a well-formed `llm_scan`, then `POST /query` and asserts the one response
  carries both scans. Prints the worked-example curl req/resp above. Live —
  one real OpenRouter call, one real `set_payload`.
- **`app/tests/test_scan_index.py`** — hermetic (LLM mocked, in-memory
  QdrantClient): scan-and-write, `skipped`/`force`, rescan on content change,
  rescan on stale verdict, `content_hash` lookup, 404 / 503 routes.
- **`app/tests/test_scan_service.py`** — the pure `/scan` path (pre-existing).
- **`app/smoke_scan.py`** — one-shot live `/scan` check (pre-existing).

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
7. `curl :8000/query -d '{"query":"homelab pihole dns","asset_type":"skill"}'`
   — the hit carries a `llm_scan` object.
8. `uv run pytest test_scan_top_skills.py test_index_qdrant_publications.py`
   plus the existing `app/tests/test_scan_index.py`.
