# LLM scanning — project plan & status

Living status doc for adding a **non-deterministic LLM threat scan** as a
pipeline step: select the top skills from Qdrant, scan each through the
FastAPI service, and persist the verdict onto the skill's Qdrant point.

Design detail: [`docs/ARCHITECTURE_LLM_SCAN.md`](docs/ARCHITECTURE_LLM_SCAN.md).
Sibling context: the Vettd deterministic scan
([`docs/ARCHITECTURE_PUBLISHING_SCANS.md`](docs/ARCHITECTURE_PUBLISHING_SCANS.md))
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
- **End-to-end smoke test** — `smoke_scan_top_skills.py` (repo root). Proves
  the whole loop against the **live** `agent_skills` collection:
  1. scroll Qdrant for the highest-star skill with a Vettd **security** finding
     (`locations[].vettd_scan_findings.categories_flagged ∋ "security"`),
     excluding openclaw/hermes;
  2. scan its `content` via the `/scan` HTTP API (spawns a throwaway
     `uvicorn query_service:app` if nothing serves `/scan`; key falls back to
     `../skill-scan-eval/.env`);
  3. write the verdict to that point as a top-level `llm_scan` field via
     `set_payload`;
  4. read the point back and assert `llm_scan` is present and well-formed.

  Last run: target `crabbox` (`steipete/clawdis`, point
  `39fc8269-f014-51e3-858c-42316cf1465b`, 386k stars), deepseek, 3 findings,
  ~28s, **PASS**. One real write was left on that point (smoke test prints the
  `delete_payload` undo one-liner).
- **Design + pipeline docs** — `docs/ARCHITECTURE_LLM_SCAN.md` (this step),
  pointer from `docs/ARCHITECTURE.md`, "Running this now" in
  `docs/ARCHITECTURE_LLM_SCAN.md` and a section in `DAILY_JOB.md`.
- **Selection query patterns** — `TEST_PLAN_FINDINGS_SUMMARY_TOP1000.md` §5
  (top-N by stars; skills with Vettd security findings; the
  `agent_compatibility` degraded-classifier caveat).
- **Vettd findings rollup** — `publish_scans.py` now writes
  `locations[].vettd_scan_findings` (`overall_grade`, `severity_counts`,
  `categories_flagged`, `has_malicious_findings`, `top_findings`) alongside the
  receipts, which is what makes the security-signal selection filter possible.

### Not done

- `scan_top_skills.py` — the selection + fan-out step.
- `POST /scan/skill` — the endpoint that also **writes** `llm_scan` to Qdrant
  (see the decision below).
- `_preserve_scan_publications()` extension for `llm_scan` in `index_qdrant.py`.
- `/query` (`SkillHit`) + `openapi.json` + Next.js docs for `llm_scan`.
- Any wiring into `batch_pipeline.py` / `RUN.sh`.
- Committing the uncommitted `/scan` work.

### Observations from the smoke run

- deepseek returned `primary_threats` as full sentences rather than the short
  threat-type names the schema expects, and `max_severity` came out `LOW`
  despite the prose reading more serious — the model isn't strictly honoring
  the prompt's AITech severity taxonomy. Model/prompt choice is tracked in
  `../skill-scan-eval/PROMPT_SELECTION_CURRENT.md`; revisit before a wide run.
- The long-running `:8000` (docker) / `:8001` (local `.venv`) query services
  predate the `/scan` wiring and don't expose it — a real run needs a fresh
  `uvicorn`.

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
- **There must be a smoke test for that API**: `POST /scan/skill {point_id}`
  → assert the *endpoint* wrote a well-formed `llm_scan` to Qdrant → re-POST
  → assert `skipped: true`. Retarget `smoke_scan_top_skills.py` at
  `/scan/skill` once it exists (it currently does the Qdrant write itself).

### 2. Pipeline docs must make it clear how to run this now

- `docs/ARCHITECTURE_LLM_SCAN.md` → "Running this now" section (done).
- `DAILY_JOB.md` → LLM-scan section with the concrete commands (done).
- `RUN.sh` header → note the step exists but is opt-in / not yet wired, and
  point at this plan (done).
- Keep these updated as `scan_top_skills.py` / `--with-scan` land.

---

## Roadmap

| # | Item | Notes |
|---|---|---|
| 1 | ~~Commit the `/scan` work~~ | done — `a658323` |
| 2 | `POST /scan/skill` (scan + write `llm_scan`) | new-requirement #1; service becomes a scoped Qdrant writer |
| 3 | Retarget the smoke test at `/scan/skill` | new-requirement #1; assert the endpoint did the write; add the `skipped:true` re-POST case |
| 4 | `prompt_version` on `ScanResponse` | `sha256(prompt)[:12]`; needed for rescan gating |
| 5 | `scan_top_skills.py` — select + fan out | stars desc, Vettd-security filter, exclude openclaw/hermes, bounded concurrency, `--dry-run` / `--force` / `--top-n` |
| 6 | `_preserve_scan_publications()` → also carry `llm_scan` | `index_qdrant.py`; add `"llm_scan"` to the `retrieve` `with_payload` |
| 7 | `test_scan_top_skills.py` + extend `test_index_qdrant_publications.py` | mirror `test_publish_scans*.py` |
| 8 | `/query` `SkillHit` + `openapi.json` + `NEXTJS_INTEGRATION.md` / `QUERY_INTERFACE.md` | so the frontend can render the real verdict |
| 9 | `export_csv.py` severity/count columns | follow-up |
| 10 | Wire `--scan-top-skills` into `batch_pipeline.py`; opt-in `--with-scan` in `RUN.sh` | run once after the final index |

## Out of scope

- MCP servers (`mcp_servers` collection).
- `llm_scan` history — latest verdict only.
- Gating selection on real Vettd *severity* (needs `publish_scans.py` to persist
  per-finding severity, not just the rollup).
