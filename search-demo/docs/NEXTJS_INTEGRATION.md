# Calling Qdrant from Next.js

How a Next.js app reads the `agent_skills` Qdrant collection: the shape of
the data, how the embeddings work, and — the thing to get right before
anything else — **how the collection gets populated, which is not this.**

## Read this first: Next.js does not populate Qdrant

`agent_skills` is written **exclusively** by this repo's Python batch
pipeline (`clone_repos.py` → `extract_search_raw.py` → `index_qdrant.py`,
see [`README.md`](../README.md#pipeline) and
[`DAILY_JOB.md`](../DAILY_JOB.md)), run on a **recurring/nightly schedule**
(by hand or cron — see `DAILY_JOB.md` and `RUN.sh`), entirely outside of and
independently from any Next.js deployment.

A Next.js app is a **read-only query-time consumer**. Concretely:

- Next.js code should never call `upsert`, `set_payload`, `delete`,
  `create_collection`, or any other write endpoint against this collection.
- Next.js has no copy of `search-raw/`, `repo-seeds/registry.json`, or any
  other pipeline input, and shouldn't need one — it only ever talks to the
  already-built collection over the network.
- "The index looks stale" is a batch-job scheduling/monitoring problem
  (check `DAILY_JOB.md`'s step 0, `registry.py unsynced`, cron logs), not
  something a page load or API route should try to fix by re-indexing.
  Never trigger `index_qdrant.py` (or anything upstream of it) from
  request-handling code, a cron-less serverless function, or a build step.
- A missing or zero-point collection is an **onboarding/outage state** for
  the batch job, not "no search results" — surface it distinctly (see
  "Empty or missing collection" below) rather than rendering it as a normal
  empty query.

Everything below describes what Next.js can *read*, never what it can write.

## Connecting: Next.js must use the Docker/server mode, not the embedded store

[`docs/QUERY_INTERFACE.md`](QUERY_INTERFACE.md#collection) documents two
connection modes the Python side supports: a Docker Qdrant server
(`QdrantClient(url=...)`) and a local **embedded**, in-process, on-disk
store (`QdrantClient(path=...)`, Python-only, `qdrant_client`'s local mode).

**Only the Docker/server mode is reachable from Next.js.** The embedded
store is a Python-in-process feature of `qdrant_client` — there is no
Node/edge equivalent, and it takes an exclusive file lock that a
long-running Next.js server couldn't safely share with the batch job
anyway. So:

- Point Next.js at a running Qdrant server, e.g.
  `QDRANT_URL=http://localhost:6333` in dev, or wherever the shared
  server/Docker instance lives in staging/prod — the **same** server
  `index_qdrant.py`'s batch job writes into (`SKILLS_QDRANT_URL` on the
  Python side; see `docs/QUERY_INTERFACE.md`'s connection-mode table).
  Reading from a different server/collection than the batch job writes is
  two independent stores, not an out-of-date copy of the same one.
- Use the official JS/TS client, `@qdrant/js-client-rest` (REST) or
  `@qdrant/js-client-grpc` (gRPC), from a server-side context (a Route
  Handler, Server Component, or Server Action) — never expose the Qdrant
  URL/port to the browser directly.
- No API key is required against a local/self-hosted server by default;
  if the shared server requires one, pass it the same way `api_key` works
  in the Python client.

## Nature of the embeddings — and why Next.js can't just call `query_points` the way `app/search.py` does

`app/search.py`'s `search_skills()` (the Python reference implementation,
see `docs/QUERY_INTERFACE.md#querying`) passes a `models.Document(text=...,
model=...)` object as the query and lets `qdrant_client` embed it
automatically. **That convenience is a Python-client-side feature of
`qdrant_client`+`fastembed`, not a Qdrant server feature and not something
the JS client provides.** Against a self-hosted open-source Qdrant server
(what this collection lives on), embedding always happens in whichever
client library issues the request — the server only ever stores and
searches numeric vectors. So a plain `@qdrant/js-client-rest` call with a
raw query string in place of a vector will not work; you must supply
already-computed vectors.

Two named vectors exist per point, and a Next.js query needs both to
reproduce the same hybrid search as the Python CLI/Streamlit app:

| Vector | Model | Shape | Distance | What it captures |
|---|---|---|---|---|
| `dense` | `sentence-transformers/all-MiniLM-L6-v2` | 384-dim float array | cosine | semantic similarity |
| `sparse` | `Qdrant/bm25` (IDF modifier) | sparse `{indices: int[], values: float[]}` | dot-product (BM25 scoring) | exact lexical/keyword matching |

Both are produced locally by `fastembed` (onnxruntime) — there's no
external embedding API, no API key, and no network call for embedding
itself; the cost is CPU/model-load, not latency-to-a-third-party.

**What gets embedded differs between index time and query time** — this
asymmetry matters if you're trying to match Python's relevance behavior
exactly:

- **At index time** (`index_qdrant.py`), both the dense and sparse vectors
  for a point are computed from the *same* composed string:
  `f"{name}: {description}\n\n{content}"` — the skill's name and
  description prefixed onto its full `SKILL.md` text.
- **At query time** (`app/search.py`), both vectors are computed from the
  *raw, stripped* user query string — no prefixing, no template.

Two ways to get equivalent query vectors from Next.js, in order of
preference:

1. **Recommended, and already built: `app/query_service.py`.** A thin
   read-only FastAPI service that imports `app/search.py` directly and
   calls `search_skills()`/`browse_skills()` — zero duplicated
   embedding/query logic, byte-identical relevance to the CLI/Streamlit
   reference implementation. Have your Next.js Route Handler call it over
   HTTP.

   - **Endpoint**: `POST /query` — body `{query, limit, min_stars,
     sources, rank_filters, languages, agent_compatibility}` (all but
     `query` optional), returns `{index_ready, query, hits: [...]}`. Each
     hit is the full payload shape from the table below plus `score` (RRF
     fused score, not a similarity) and `rank`. An empty `query` browses
     instead of searching (same fallback `search_skills()`/`browse_skills()`
     use). `languages`/`agent_compatibility` are native Qdrant filters
     (`MatchAny`) that compose (AND) with every other filter and with the
     query itself — same push-down pattern as `sources`/`min_stars`.
   - **Health check**: `GET /health` → `{"index_ready": bool}` — use this
     (or the `index_ready` field on `/query` responses) for the "Empty or
     missing collection" case below instead of treating an empty `hits`
     list as "no results."
   - **Connects to Qdrant the same way this repo's batch job does**: server
     mode via `SKILLS_QDRANT_URL` (default `http://localhost:6333`),
     never the embedded `path=` mode — see `app/search.py`.
   - **OpenAPI schema**: served automatically at `/openapi.json` (and
     `/docs` for interactive exploration) since it's FastAPI — generate a
     typed client on the Next.js side from that.
   - **Local dev**: `docker compose -f docker/docker-compose.qdrant.yml up
     -d` (or `docker/deploy-qdrant-docker.sh`) brings up both `qdrant`
     (ports 6333/6334) and `query-service` (port 8000) together, wired via
     the compose network. Point Next.js at `http://localhost:8000`.
   - **Remote deploy**: same compose file/`deploy-qdrant-docker.sh` script
     is this repo's only deploy path today — run it on whatever host runs
     the shared Qdrant server, and point Next.js at that host's port 8000.
   - Can also be run directly without Docker: `cd app && uv run uvicorn
     query_service:app --host 0.0.0.0 --port 8000`.
2. **Advanced: re-embed independently in Node.** Load the identical models
   (`sentence-transformers/all-MiniLM-L6-v2` for dense,
   `Qdrant/bm25` for sparse) via an ONNX-capable JS runtime (e.g.
   `@xenova/transformers` / `onnxruntime-node`) and replicate the exact
   tokenization/pooling/IDF logic `fastembed` uses, then call
   `@qdrant/js-client-rest`'s `queryPoints` directly with the computed
   dense array and sparse `{indices, values}` in place of a `Document`.
   This avoids the extra hop but doubles the surface area that has to stay
   in sync with the Python side (model versions, preprocessing, IDF
   statistics) — any drift silently changes relevance rather than erroring.
   Only take this path if the proxy hop in option 1 is a proven problem.

## Collection & query shape

- **Collection name**: `agent_skills`
- **Query pattern**: hybrid dense + sparse search, fused with Reciprocal
  Rank Fusion (RRF) — see `docs/QUERY_INTERFACE.md#querying` for the exact
  `query_points` shape (prefetch limits, fusion, filters) to mirror.
  `hit.score` after RRF is a **fused rank score, not a 0–1 similarity** —
  don't render it as a percentage.
- **Filtering**: `min_stars`, `sources`, and `search_rank_<source>_<metric>`
  filters are native Qdrant `FieldCondition`s (see
  `docs/QUERY_INTERFACE.md`'s ranking-metadata section) — apply them via
  the request's `filter`/`query_filter`, not by fetching everything and
  filtering client-side in the Next.js layer.

## Payload (metadata) shape

Every point's payload is a flat JSON object. The fields a typical frontend
actually needs map directly onto `app/search.py`'s `SkillPayload`:

| Field | Type | Notes |
|---|---|---|
| `path` | string | file path relative to `search-raw/`, e.g. `owner/repo/skills/foo/SKILL.md` |
| `owner` | string | GitHub org/user |
| `repo` | string | repo name; one repo can contribute many skills |
| `repo_url` | string | `https://github.com/{owner}/{repo}` |
| `skill_url` | string | direct GitHub link to the `SKILL.md`; use this for the "view source" link, don't reconstruct a URL from other fields |
| `name` | string | short slug/title |
| `description` | string | plain text, `""` if absent — fall back to showing a `content` snippet in that case |
| `content` | string | full raw `SKILL.md` text, frontmatter included |
| `sources` | string[] | discovery channels (`seed`/`search`/`manual`/`marketplace`); can be empty |
| `stars` | int \| null | GitHub star count of the primary location |
| `ranking` | string | flattened `key=value` ranking/popularity tokens — see `docs/QUERY_INTERFACE.md`'s ranking-metadata section before parsing this by hand |
| `search_rank_<source>_<metric>` | int (dynamic, 0+ fields) | native top-level fields for the same ranking data, filterable — present only where data exists |
| `duplicate_count` | int | how many `locations` collapsed into this one point |
| `name_collision_count` | int | other points sharing this `name` with *different* content — never silently merged, see `docs/QUERY_INTERFACE.md` |
| `name_shared_with` | string[] | `owner/repo` list for the above |
| `content_hash` | string | sha1 of whitespace-normalized `content`; internal to the indexer's incremental-update logic, not generally useful to a frontend |
| `locations` | object[] | full list of every `(owner, repo, path, ...)` this content was found at; `path`/`owner`/`repo`/etc. above are just the primary (most-starred) one flattened out |
| `language` | string | spoken/content language of the SKILL.md text (e.g. `en`, `ja-JP`, `zh-CN`), parsed from a `docs/<locale>/skills/...` translation-mirror path segment where present -- **not** the source repo's programming language; defaults to `"en"` |
| `agent_compatibility` | string[] | agent runtimes/tools this skill declares or is inferred to target (e.g. `claude-code`, `cursor`, `codex`, `generic`); `[]` when nothing was detected — real rule-based signal (plugin manifests, `agents/*.yaml` sidecars, path conventions, name mentions), never a fabricated guess, see `agent_target.py` and `docs/QUERY_INTERFACE.md`'s payload table |

Point IDs are a `uuid5` derived from `content_hash`, **not** from `path` —
identical `SKILL.md` content at two different paths collapses to the same
point ID and payload, with both locations recorded in `locations`. Don't
assume one point ID per file path.

Treat unknown/additive fields as ignorable and don't assume this list is
exhaustive going forward — new `search_rank_*` fields in particular appear
automatically as new (query, sort) ranking sources are added upstream, see
`docs/QUERY_INTERFACE.md`.

## Empty or missing collection

If the collection doesn't exist yet, or has zero points, that means the
nightly batch job hasn't completed a run yet (first-time setup) or is
broken — not "no results for this query." Detect it explicitly (e.g. a
`collectionExists`/point-count check on startup or a cached health check)
and show an onboarding/outage message rather than an empty results list;
see `docs/QUERY_INTERFACE.md`'s compatibility rules for the equivalent
guidance on the Python/Streamlit side.

## Keeping this in sync

This document describes the *consumer-side* contract; the producer-side
contract (payload fields, vector config, embedding text, incremental
update rules) is owned by `index_qdrant.py` and documented authoritatively
in [`docs/QUERY_INTERFACE.md`](QUERY_INTERFACE.md) — when that document's
payload table, querying section, or connection-mode table changes, treat
this document as needing the same review, since every field/behavior
described here is copied from (and must stay consistent with) that one.
