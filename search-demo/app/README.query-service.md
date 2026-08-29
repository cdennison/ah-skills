# Query & Scan Service

A small **FastAPI** app (`query_service.py`) that puts two things behind HTTP:

1. **Search** — the repo's local skill / MCP-server index (the same hybrid
   dense + BM25 reciprocal-rank fusion as `query.py` and the
   [Streamlit UI](README.md)), so non-Python callers (e.g. a Next.js route
   handler) can query it without reproducing the query-time embedding step.
2. **Scan** — a *non-deterministic* threat analysis of a blob of skill text:
   the text is sent to an LLM (via **litellm**, with **OpenRouter** as the
   initial provider) using the Cisco skill-scanner threat-analysis prompt,
   and the model's structured verdict is returned.

It is part of the [agent-skills-search pipeline](../README.md); see that
README for how the underlying `qdrant_db/` index is built.

Read-only with respect to Qdrant — it never writes to either collection.
Only `POST /scan` makes an outbound network call (to the LLM provider).

## Endpoints

| Method / path | What it does |
|---|---|
| `GET /health` | Reports whether the requested collection is populated (`asset_type` = `skill` \| `mcp`). |
| `POST /query` | Read-only hybrid search over the `agent_skills` / `mcp_servers` collections. Body: `{query, asset_type, limit, …filters}`. See [`docs/NEXTJS_INTEGRATION.md`](../docs/NEXTJS_INTEGRATION.md). |
| `POST /scan` | Non-deterministic LLM threat scan. Body: `{skill_text, skill_name?, model?}`. Returns `{model, findings[], overall_assessment, primary_threats}` — the same JSON shape `skill-scan-eval/scanner.py` produces from a skill directory. `503` if no API key configured, `502` on upstream/parse failure. |

## OpenAPI spec

- **Live**: `GET /openapi.json`, Swagger UI at `/docs`, ReDoc at `/redoc`.
- **Checked-in copy**: [`openapi.json`](openapi.json) — regenerate after any
  route or model change with:

  ```bash
  cd app
  uv run python -c "import json, query_service; json.dump(query_service.app.openapi(), open('openapi.json','w'), indent=2); open('openapi.json','a').write('\n')"
  ```

## Run

```bash
uv sync --project app
uv run --project app uvicorn query_service:app --host 0.0.0.0 --port 8000
```

Or via `docker/docker-compose.qdrant.yml` (the `Dockerfile` here builds this
service). Point clients at `http://localhost:8000`.

## `/scan` configuration

| env var | purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter key — **required** for `/scan` |
| `SKILL_SCANNER_LLM_API_KEY` | optional; takes precedence over `OPENROUTER_API_KEY` |
| `SKILL_SCANNER_LLM_MODEL` | optional litellm model id (default `openrouter/deepseek/deepseek-v3.2`, per `skill-scan-eval/PROMPT_SELECTION_CURRENT.md`) |

`prompts/skill_threat_analysis_prompt.md` is a byte-for-byte mirror of the
repo-root `../../prompts/skill_threat_analysis_prompt.md` — vendored so the
app stays self-contained for its Dockerfile. Keep the two in sync.

## Tests

`tests/test_scan_service.py` and `tests/test_search.py` are hermetic — the
scan tests mock the LLM call, no network:

```bash
cd app
uv run ruff check .
uv run basedpyright
uv run python -m pytest -q
```

## Smoke test

[`smoke_scan.py`](smoke_scan.py) is a one-shot **live** check — it is *not*
collected by pytest and makes exactly one paid OpenRouter call. It drives the
full `/scan` path in-process (validation → litellm → OpenRouter → schema
parse → `ScanResponse`) with a deliberately malicious toy skill and prints
the findings.

```bash
cd app
uv run python smoke_scan.py   # reads OPENROUTER_API_KEY, else ../../skill-scan-eval/.env
```
