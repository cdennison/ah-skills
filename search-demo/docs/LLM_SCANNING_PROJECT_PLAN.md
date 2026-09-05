# LLM scanning — project plan & status

Living status doc for adding a **non-deterministic LLM threat scan** as a
pipeline step: select the top skills from Qdrant, scan each through the
FastAPI service, and persist the verdict onto the skill's Qdrant point.

Design detail: `vettd-e2e/docs/specs/architecture-llm-scan.md`.
Sibling context: the Vettd deterministic scan
(`vettd-e2e/docs/specs/architecture-publishing-scans.md`)
and the prompt eval harness (`../skill-scan-eval/`).

---

## Where we are (2026-08-29)

### Done

- **`POST /scan` endpoint** — `app/scan_service.py` + `app/query_service.py`
  (committed, `a658323`; `app/tests/test_scan_service.py` hermetic,
  `app/smoke_scan.py` one-shot live). Pure `skill_text → verdict`: runs the
  Cisco threat-analysis prompt through litellm/OpenRouter (default
  `openrouter/deepseek/deepseek-v3.2`, no temperature/seed), returns
  `{model, findings[], overall_assessment, primary_threats}`. Touches no
  Qdrant. `litellm` is in `app/.venv`.
- **`POST /scan/skill` endpoint** — `app/scan_index.py::scan_and_record`, wired
  in `app/query_service.py` (v1.3.0). Looks up an `agent_skills` point by
  `point_id` (or `content_hash`), rescan-gates against the existing `llm_scan`
  (content sha + model + `prompt_version` + age; `force` bypasses), runs the
  same scan as `/scan`, and **writes the `llm_scan` payload key itself** via
  `set_payload` — the service's only Qdrant write. Adds `prompt_version` to
  `ScanResponse`. Hermetic tests in `app/tests/test_scan_index.py` (LLM mocked,
  in-memory QdrantClient).
- **`/query` exposes `llm_scan`** — `SkillHit.llm_scan` (nullable `LlmScan`
  object), through `search.SkillPayload` / `SearchResult` / `_to_skill_hit`.
  Vettd data already rides in `SkillHit.locations`, so one `/query` response
  now carries both scans. `app/openapi.json` regenerated.
- **End-to-end smoke test** — `smoke_scan_skill.py` (repo root), hard-coded to
  `affaan-m/everything-claude-code/skills/homelab-pihole-dns/SKILL.md` (point
  `83a8b1b8-…`), a skill with a **published** Vettd scan (grade B, VTD-0088
  security finding) that is **not** openclaw/hermes-related (asserted: name,
  agent_compatibility, and every location checked). It: (1) confirms the Vettd
  `vettd_scan_findings` + `vettd_scan_publications` on the point; (2)
  `POST /scan/skill {point_id, force}` — asserts the endpoint returned a
  well-formed `llm_scan`; (3) `POST /query "homelab pihole dns"` — asserts the
  one hit carries **both** `llm_scan` and the Vettd scan. Spawns a throwaway
  service if none serves `/scan/skill`; key falls back to
  `../skill-scan-eval/.env`. **PASS** (deepseek, ~16s, one real `set_payload`).
  Worked-example curl req/resp is in `vettd-e2e/docs/specs/architecture-llm-scan.md`.
- **Design + pipeline docs** — `vettd-e2e/docs/specs/architecture-llm-scan.md` (this step, incl.
  worked example), pointer from `docs/ARCHITECTURE.md`, "Running this now" +
  `DAILY_JOB.md` section.
- **Selection query patterns** — `TEST_PLAN_FINDINGS_SUMMARY_TOP1000.md` §5
  (top-N by stars; skills with Vettd security findings; the
  `agent_compatibility` degraded-classifier caveat).
- **Vettd findings rollup** — `publish_scans.py` now writes
  `locations[].vettd_scan_findings` (`overall_grade`, `severity_counts`,
  `categories_flagged`, `has_malicious_findings`, `top_findings`) alongside the
  receipts, which is what makes the security-signal selection filter possible.

### Not done

- `scan_top_skills.py` — the selection + fan-out step (select point ids →
  `POST /scan/skill` per skill, bounded concurrency).
- `_preserve_scan_publications()` extension for `llm_scan` in `index_qdrant.py`.
- `NEXTJS_INTEGRATION.md` / `QUERY_INTERFACE.md` prose for the `llm_scan` field
  (the field + `openapi.json` are done; the prose isn't).
- `export_csv.py` severity/count columns.
- Any wiring into `batch_pipeline.py` / `RUN.sh`.

### Observations from the smoke runs

- The scan is genuinely non-deterministic: back-to-back runs of the same skill
  returned different `max_severity` / `finding_count` (e.g. `LOW`/1, `MEDIUM`/4,
  `NONE`/0). `primary_threats` also come back as free-text phrases, not the
  prompt's short threat-type names. Model/prompt
  choice is tracked in `../skill-scan-eval/PROMPT_SELECTION_CURRENT.md`; revisit
  before a wide run.
- The long-running `:8000` (docker) / `:8001` (local `.venv`) query services
  predate this wiring and don't expose `/scan` or `/scan/skill` — a real run
  needs a fresh `uvicorn` (or let the smoke test spawn its own).

---

## New requirements (added 2026-08-29)

### 1. All scanning **and** the Qdrant upload sit behind one API call

The pipeline step must not write to Qdrant itself. Add **`POST /scan/skill`**
(`{point_id | content_hash, model?, force?}`): the service looks up the point,
reads its `content`, applies rescan gating, scans, writes the `llm_scan`
payload field, and returns `{point_id, skipped, llm_scan}`. `scan_top_skills.py`
then only *selects* point ids and calls this endpoint per skill.

- `POST /scan` stays pure (text → verdict) for the eval harness and ad-hoc use.
- This makes the FastAPI service a Qdrant **writer** for the first time —
  scoped to the single `llm_scan` key via `set_payload`, never vectors or
  collection creation.
- **There must be a smoke test for that API** — done: `smoke_scan_skill.py`
  hits `POST /scan/skill` and asserts the *endpoint* did the write, then shows
  it via `POST /query`.

### 2. Pipeline docs must make it clear how to run this now

- `vettd-e2e/docs/specs/architecture-llm-scan.md` → "Running this now" section (done).
- `DAILY_JOB.md` → LLM-scan section with the concrete commands (done).
- `RUN.sh` header → note the step exists but is opt-in / not yet wired, and
  point at this plan (done).
- Keep these updated as `scan_top_skills.py` / `--with-scan` land.

---

## Roadmap

| # | Item | Notes |
|---|---|---|
| 1 | ~~Commit the `/scan` work~~ | done — `a658323` |
| 2 | ~~`POST /scan/skill` (scan + write `llm_scan`)~~ | done — `app/scan_index.py`; scoped `set_payload` |
| 3 | ~~Smoke test for that API~~ | done — `smoke_scan_skill.py` + `app/tests/test_scan_index.py` |
| 4 | ~~`prompt_version` on `ScanResponse`~~ | done — `sha256(prompt)[:12]` |
| 5 | ~~`/query` `SkillHit.llm_scan` + `openapi.json`~~ | done; `NEXTJS_INTEGRATION.md` / `QUERY_INTERFACE.md` prose still open |
| 6 | `scan_top_skills.py` — select + fan out | stars desc, Vettd-security filter, exclude openclaw/hermes, bounded concurrency, `--dry-run` / `--force` / `--top-n`; per skill → `POST /scan/skill` |
| 7 | `_preserve_scan_publications()` → also carry `llm_scan` | `index_qdrant.py`; add `"llm_scan"` to the `retrieve` `with_payload` |
| 8 | `test_scan_top_skills.py` + extend `test_index_qdrant_publications.py` | mirror `test_publish_scans*.py` |
| 9 | `export_csv.py` severity/count columns | follow-up |
| 10 | Wire `--scan-top-skills` into `batch_pipeline.py`; opt-in `--with-scan` in `RUN.sh` | run once after the final index |

## Out of scope

- MCP servers (`mcp_servers` collection).
- `llm_scan` history — latest verdict only.
- Gating selection on real Vettd *severity* (needs `publish_scans.py` to persist
  per-finding severity, not just the rollup).
