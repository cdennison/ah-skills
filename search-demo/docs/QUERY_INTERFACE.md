# Qdrant query interface

How the `agent_skills` collection is shaped and how to query it — for anyone
wiring up a new frontend (Streamlit, etc.) on top of `qdrant_db/`.

## Collection

- **Path**: `qdrant_db/` (local, on-disk, embedded — no server process; open
  it directly with `QdrantClient(path="qdrant_db")`)
- **Name**: `agent_skills`
- **Vectors**: two named vectors per point
  - `dense` — `sentence-transformers/all-MiniLM-L6-v2`, cosine distance, semantic similarity
  - `sparse` — `Qdrant/bm25`, exact lexical/keyword matching
- Built and populated by `index_qdrant.py`; **read-only** from a frontend's
  perspective — re-run that script to rebuild, don't write from the app.

## Payload (metadata) fields

Every point's payload is a flat dict with these fields, set in
`index_qdrant.py`'s `load_skills()`:

Fields marked **primary-location** are the single "best" `(owner, repo, path, …)`
tuple flattened out of `locations[]` — `index_qdrant._primary_location()` picks
it most-starred first, then alphabetically. The complete list of every place
this exact content was found lives in `locations[]`.

| Field | Source | Notes |
|---|---|---|
| `id` | `str(uuid5(POINT_ID_NAMESPACE, content_hash))` — `index_qdrant.point_id()` | stable point id (Qdrant requires int or UUID). Derived from **`content_hash`, not `path`** — identical `SKILL.md` content at two different paths collapses to one point (both recorded in `locations[]`). Also stored as a payload field, not just the Qdrant point id. Used to upsert/delete on re-index without disturbing unrelated points |
| `path` | primary-location file path relative to `search-raw/`, e.g. `owner/repo/skills/foo/SKILL.md` | breadcrumb-style identifier for the primary location; every location's path is in `locations[]` |
| `owner` | primary-location `rel.parts[0]` | GitHub org/user that owns the (primary) repo |
| `repo` | primary-location `rel.parts[1]` | one repo can contribute many skills; other repos carrying this same content are in `locations[]` |
| `repo_url` | `https://github.com/{owner}/{repo}` (primary location) | link to the repo itself |
| `skill_url` | `https://github.com/{owner}/{repo}/blob/HEAD/{subpath}` (primary location) | direct link to the `SKILL.md` (or extra README) on GitHub; `blob/HEAD` resolves to whichever branch is currently default, so it survives a `main`/`master` rename |
| `name` | frontmatter `name:`, falls back to the parent directory name | short slug/title |
| `description` | frontmatter `description:` | **plain-text only** (see below), empty string if the file has no description |
| `sources` | union of every location's `registry.source_types()` for its `owner/repo` in `repo-seeds/registry.json` | sorted list of discovery channels (`seed`/`search`/`manual`/`marketplace`) that surfaced the repo; empty list if the repo isn't in the registry (shouldn't normally happen) — see the caveat below |
| `content` | full raw file text, frontmatter included | used for embedding + full-text display/preview |
| `content_hash` | `sha1(` whitespace-normalized `content)` hex digest — `index_qdrant.content_hash()` | whitespace runs are collapsed before hashing so two copies differing only by wrapping still dedupe; the original (un-normalized) text is what `content` stores and what gets embedded. Lets `index_qdrant.py` detect unchanged files and skip re-embedding them |
| `stars` | primary-location GitHub star count | `int` or `null` (unknown). Filterable via `min_stars` (native `Range` push-down) |
| `ranking` | primary-location flattened `key=value` popularity string, e.g. `skills_sh_rank=2799 skills_sh_top_installs=4038 search_rank_agent_skills_stars=12` — `index_qdrant._ranking_string()` | space-separated tokens; parsed by `app/search.py`'s `parse_search_rank()` and `export_csv.py`. Includes `search_rank_<query_slug>_<sort_slug>=N` tokens mirroring the native fields below |
| `search_rank_<query_slug>_<sort_slug>` | `index_qdrant._search_rank_fields()` from the primary repo's registry entry | **dynamic, 0+ top-level `int` fields, present only where data exists.** The skill's rank (0 = best) in a specific upstream `(search query, sort)` list. Native fields so they're usable in a Qdrant `FieldCondition` — `/query`'s `rank_filters` (`{metric: max_rank}`) pushes these down. New (query, sort) sources add new field names automatically |
| `duplicate_count` | `len(locations)` | how many `locations[]` entries collapsed into this one point (1 = found in exactly one place) |
| `name_collision_count` | `index_qdrant.load_skills()` post-pass | count of **other** points that share this `name` but have **different content** — never silently merged (could be a coincidental generic name or a reworded fork); `0` normally |
| `name_shared_with` | same post-pass | sorted `owner/repo` list for those colliding points; `[]` normally |
| `locations` | every `(owner, repo, path, repo_url, skill_url, sources, stars, ranking, language, agent_compatibility, …)` this exact content was found at | `object[]`. The flattened top-level `owner`/`repo`/`path`/… above are just the primary entry. Also where the **deterministic Vettd scan** rides: `locations[].vettd_scan_findings` (grade/trust/severity rollup) and `locations[].vettd_scan_publications` (ingest receipts) — per-location, preserved separately from the top-level scan keys. See [`ARCHITECTURE_PUBLISHING_SCANS.md`](ARCHITECTURE_PUBLISHING_SCANS.md) |
| `language` | `index_qdrant.py`'s `_content_language()`, parsed from a `docs/<locale>/skills/...` path segment (e.g. `docs/ja-JP/skills/...`, `docs/zh-CN/skills/...` -- the translation-mirror convention some repos use, see `affaan-m/ECC`) | **spoken/content language of the SKILL.md text**, not the source repo's programming language; `"en"` (the untranslated original) when no such path segment is present. Filterable via `/query`'s `languages` (native `MatchAny`) |
| `agent_compatibility` | `agent_target.classify_agent_target()` (filesystem-aware: plugin manifests, `agents/*.yaml` sidecars — used when the repo's `repos/<owner>/<repo>` clone is on disk) or `agent_target.classify_from_metadata()` (path/text-only fallback otherwise), unioned across every `locations` entry | sorted list of agent runtimes/tools this skill declares or is inferred to target (e.g. `claude-code`, `cursor`, `codex`, `generic`); `[]` when nothing was detected — never fabricated, see `agent_target.py`'s module docstring for the signal tiers and confidence levels. Filterable via `/query`'s `agent_compatibility` (native `MatchAny`) |
| `llm_scan` | `app/scan_index.py`'s `scan_and_record()`, written **post-index** by `POST /scan/skill` via `set_payload` (never by `index_qdrant.py`) | **optional, additive.** Absent until the skill has been scanned. Latest non-deterministic LLM threat-scan verdict, no history. Shape: `{model, prompt_version, scanned_at, content_sha256, max_severity: "CRITICAL"\|"HIGH"\|"MEDIUM"\|"LOW"\|"NONE", finding_count, primary_threats: [...], overall_assessment, findings: [{severity, aitech, title, description, aisubtech?, location?, evidence?, remediation?}]}`. See [`ARCHITECTURE_LLM_SCAN.md`](ARCHITECTURE_LLM_SCAN.md). **NOT carried across a re-index yet** (unlike `cli_security`) — `_PRESERVED_TOP_LEVEL_KEYS` does not include it until its pipeline step lands, so a verdict survives only until the next `index_qdrant.py` run |
| `cli_security` | `cli-security-scan/build_cli_export.py` (`set_payload`, post-index) — grep install commands, classify npm/pip packages as CLI, audit against OSV.dev | **optional, additive.** Absent unless the skill installs a confirmed-CLI package. `{grade: "A"\|"B"\|"C", packages: [{package, ecosystem, classification, install_command, vuln_count, max_severity, advisory_ids}], scanned_at, osv_snapshot_date}`. `grade` = worst package (C also covers an advisory OSV left unlabeled). Means "a tool this skill installs has a security history" — OSV is queried version-less — not "vulnerable". See [`ARCHITECTURE_CLI_SECURITY_SCAN.md`](ARCHITECTURE_CLI_SECURITY_SCAN.md). Carried across re-index by `index_qdrant._preserve_scan_publications` (`_PRESERVED_TOP_LEVEL_KEYS`). |

### `sources` staleness caveat

`content_hash` only hashes the `SKILL.md` text, so it does **not** change
when a repo gains a new registry source between runs (e.g. a repo already
found via `seed` later also turns up in `fetch_marketplace.py`).
`index_qdrant.py`'s incremental hash-diff will treat that point as
unchanged and skip re-uploading it, so its `sources` payload can go stale
until the next **full rebuild**. Re-run `index_qdrant.py` against a fresh
`qdrant_db/` (see the synchronization workflow below) after any registry
sync (`fetch_marketplace.py`, `registry.py sync-seed`, `add-search`,
`add-manual`) if you need `sources` to reflect the latest registry state
immediately, rather than waiting for unrelated content changes to trigger
a re-embed.

This same repo-level provenance also feeds `repo-seeds/skills.json`
(`skills_map.py`), which maps each skill *name* to every repo it was found
in plus that repo's sources — useful for spotting the same skill vendored
into multiple repos. It is refreshed by `clone_repos.py` on every run,
independently of the Qdrant index, so it doesn't share this staleness
issue.

### `description` parsing caveat

`description` comes from the YAML frontmatter block at the top of each
`SKILL.md`, parsed by `parse_frontmatter()` in `index_qdrant.py` — a
lightweight hand-rolled parser, not a real YAML library. It correctly
handles both plain scalars and multi-line block scalars:

```yaml
description: Single-line description
```

```yaml
description: |
  Multi-line block scalar.
  Line breaks are preserved as-is.
```

```yaml
description: >
  Folded block scalar.
  Line breaks become spaces.
```

If a `SKILL.md` uses any other YAML frontmatter shape for `description`
(flow sequences, nested maps, etc.) it won't be recognized and the field
will come back as an empty string — check `payload["description"] == ""`
as a signal to fall back to showing a `content` snippet instead.

## Querying

Search is **hybrid**: dense (semantic) + sparse (BM25 keyword), fused with
Reciprocal Rank Fusion (RRF). This is what `query.py` does and what any new
frontend should replicate:

```python
from qdrant_client import QdrantClient, models
from index_qdrant import COLLECTION, DB_PATH, DENSE_VECTOR_NAME, MODEL_NAME, SPARSE_MODEL_NAME, SPARSE_VECTOR_NAME

client = QdrantClient(path=str(DB_PATH))

results = client.query_points(
    collection_name=COLLECTION,
    prefetch=[
        models.Prefetch(
            query=models.Document(text=query_text, model=MODEL_NAME),
            using=DENSE_VECTOR_NAME,
            limit=20,
        ),
        models.Prefetch(
            query=models.Document(text=query_text, model=SPARSE_MODEL_NAME),
            using=SPARSE_VECTOR_NAME,
            limit=20,
        ),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    limit=n,  # final result count returned to the caller
)

for hit in results.points:
    hit.score        # RRF fusion score (not a raw cosine similarity)
    hit.payload["path"]
    hit.payload["owner"]
    hit.payload["repo"]
    hit.payload["repo_url"]
    hit.payload["skill_url"]
    hit.payload["name"]
    hit.payload["description"]
    hit.payload["sources"]
    hit.payload["content"]
```

Notes:

- `models.Document(text=..., model=...)` triggers FastEmbed to embed the
  query text automatically, on the fly, with the same models used at index
  time — no manual embedding step, no API key.
- The `prefetch` limit (20 here) is the candidate pool each sub-query pulls
  before fusion; `limit` on the outer `query_points` call is the final
  number of fused results returned. Keep prefetch limit ≥ final limit.
- `hit.score` after RRF fusion is **not** a 0–1 cosine similarity — it's a
  fused rank score. Don't render it as a percentage; use it only for
  relative ordering, or bucket it into rough tiers if you want a
  qualitative label.
- Filtering by payload (e.g. `owner == "google"` and `repo == "skills"`) can be added via
  `models.Filter` on either the outer `query_points` call or per-prefetch;
  see `client.scroll(..., scroll_filter=...)` in ad-hoc payload lookups for
  the same filter syntax.

## HTTP access (the query service)

The Python snippet above is the in-process path (Streamlit, `query.py`). For
**non-Python callers** (a Next.js Route Handler, a Go service, `curl`), the
supported entry point is the read-only FastAPI query service in `app/`
(`app/query_service.py`), and its **authoritative HTTP contract is
[`app/openapi.json`](../app/openapi.json)** — FastAPI-generated, committed, and
regenerated on every field change (`info.version` is `1.4.0` as of this
writing).

- `POST /query` with a JSON body — `asset_type` selects the collection:
  `"skill"` (default) queries `agent_skills` and returns `SkillHit[]`; `"mcp"`
  queries `mcp_servers` and returns `McpHit[]` (see the next section). Response
  is `{index_ready, query, asset_type, hits}`.
- `GET /health?asset_type=skill|mcp` → `{asset_type, index_ready}` — use this
  (or the `index_ready` field on a `/query` response) to tell a
  missing/empty collection apart from a genuine zero-match query.
- `POST /scan` (pure `text → verdict`) and `POST /scan/skill` (scan an indexed
  point and write its `llm_scan` payload field) — see
  [`ARCHITECTURE_LLM_SCAN.md`](ARCHITECTURE_LLM_SCAN.md).

Do **not** hand-transcribe the request/response field lists into another
language — generate a typed client from `openapi.json` (`/docs` serves the
interactive UI). The consumer-side guidance for the Next.js case —
connection mode, the embedding asymmetry, the repo-URL join, empty-collection
handling — is in [`NEXTJS_INTEGRATION.md`](NEXTJS_INTEGRATION.md), which reads
from this document and `openapi.json`.

## The `mcp_servers` collection

`POST /query` with `asset_type: "mcp"` searches a **separate Qdrant
collection**, `mcp_servers`, with its **own payload shape** —
`app/mcp_search.py`'s `McpPayload` / the `McpHit` schema in `openapi.json`
(`mcp_id`, `stars`, `language`, `weekly_downloads`/`monthly_downloads`,
`transport`, `registry_type`, `package_identifier`/`package_url`,
`deployment`, `has_installable_package`/`has_remote`, and the OSV
`security_*` / `security_direct_deps_*` fields). It is populated by a
**different pipeline** (`mcp-search/`, not `clone_repos.py` →
`extract_search_raw.py` → `index_qdrant.py`) and shares only the embedding
models, the hybrid-RRF query shape, and the query service. Its producer-side
contract lives in [`../mcp-search/MCP_PIPELINE.md`](../mcp-search/MCP_PIPELINE.md)
and [`../mcp-search/E2E_ARCHITECTURE.md`](../mcp-search/E2E_ARCHITECTURE.md);
the rest of this document (payload table, re-index rules, the audit below)
is about `agent_skills` only.

## Keeping the index and frontends in sync

This document is the contract between the index writer (`index_qdrant.py`),
the reference query (`query.py`), and every frontend (`app/search.py` plus its
rendering layer). A change is complete only when the contract, producer,
consumers, stored collection, and tests agree.

### Sources of truth

- `index_qdrant.py` owns the collection name, storage path, vector names,
  embedding models, payload fields, point IDs, and incremental update rules.
- This document describes that interface for consumers. Update it in the same
  change as the indexer; never document a planned schema as if it already exists.
- `query.py` is the executable reference for hybrid-search semantics and the
  default result count.
- Frontends must import the canonical constants instead of copying their string
  values. If the frontend's import path makes that awkward, move the constants
  to a dependency-free shared module used by the indexer, CLI, and frontend;
  do not create another set of literals.
- Frontend payload models must represent every field they render or use for a
  fallback/link. Unknown additive fields may be ignored, but required fields
  must not silently disappear at the parsing boundary.

### Change-impact matrix

| Change | Update together | Index action |
|---|---|---|
| Raw files added, changed, or removed under `search-raw/` | source data only; confirm the existing payload/query contract still applies | incremental `index_qdrant.py` run |
| Frontmatter parser or derived payload fields change | `index_qdrant.py`, payload table above, frontend payload model/rendering, CLI output if relevant, tests | **full rebuild** |
| Payload field renamed, removed, or changes meaning | producer, this document, all consumers, fixtures/tests, migration/release notes | **full rebuild** and coordinated frontend release |
| Dense/sparse model, vector name, vector size, distance, or sparse modifier changes | canonical constants/config, this document, `query.py`, frontend query adapter, tests | **full rebuild** |
| Hybrid query shape, fusion method, prefetch size, filters, or result default changes | querying section above, `query.py`, frontend adapter/UI controls, tests | no rebuild unless indexed vectors/payload also change |
| GitHub URL/path convention changes | payload derivation, payload table, frontend link rendering, tests | **full rebuild** because URLs are derived payload |
| Scan-verdict payload fields change (`llm_scan`, `cli_security`, `locations[].vettd_scan_*`) | the **post-index producer** (`app/scan_index.py` / `cli-security-scan/build_cli_export.py` / `publish_scans.py`), the payload table above, `SkillHit`/`openapi.json`, `NEXTJS_INTEGRATION.md`, and `index_qdrant._PRESERVED_TOP_LEVEL_KEYS` / `_preserve_scan_publications` (preservation is per-key, not automatic) | no rebuild — these are written by a separate scan step *after* `index_qdrant.py`, not by it. A re-index only preserves the keys named in `_PRESERVED_TOP_LEVEL_KEYS` (`cli_security` today, **not `llm_scan`**) plus per-`locations` `vettd_scan_*` |
| UI-only layout or copy changes | frontend and frontend tests | no rebuild |

The indexer decides whether a point changed from the raw file's `content_hash`.
That catches source-file edits, but it does **not** detect changes to parser
logic, derived URLs, payload shape, embedding text construction, model choice,
or vector configuration when the raw file is unchanged. Until the index stores
and checks an explicit schema/index version, treat every such change as a full
rebuild rather than relying on the incremental path.

### Synchronization workflow

1. **Stop every process using `qdrant_db/`.** Local embedded Qdrant takes an
   exclusive path lock. Stop Streamlit before indexing or rebuilding; do not
   run `query.py` concurrently with the app. If concurrent independent
   processes become a requirement, run Qdrant as a server instead of local
   mode — see `docker/docker-compose.qdrant.yml`.
2. **Update the contract and producer together.** Change `index_qdrant.py` and
   the collection/payload/query sections in this document in the same review.
3. **Update all consumers.** Keep `query.py`, the frontend query adapter,
   payload types, result-limit controls, fallbacks, links, labels, and empty
   collection behavior aligned with the revised contract.
4. **Choose incremental or full indexing using the matrix above.** For a full
   rebuild, preserve the old store until verification succeeds:

   ```bash
   mv qdrant_db qdrant_db.pre-interface-change
   .venv/bin/python index_qdrant.py
   ```

   Restore the backup if verification fails. Remove it only after both CLI and
   frontend checks pass.
5. **Verify the stored collection before starting Streamlit.** Confirm the
   collection exists, contains points, exposes the expected named vectors, and
   that sampled payloads contain every required field with the documented
   meanings.
6. **Run the reference CLI and frontend checks:**

   ```bash
   .venv/bin/python query.py "excel spreadsheets" -n 5
   uv sync --project app
   cd app
   uv run ruff check .
   uv run basedpyright
   uv run python -m pytest -q
   cd ..
   uv run --project app streamlit run app/streamlit_app.py
   ```

7. **Exercise the real UI.** Verify a normal search, an exact-keyword search,
   the configured result-count boundaries, an empty query, a missing or empty
   collection, a payload with an empty description, RRF-score wording, and the
   generated GitHub links.

### Compatibility rules

- Additive optional payload fields are backward-compatible only when older
  consumers do not require them. Update consumers before making an added field
  required.
- Renames, removals, type changes, and meaning changes are breaking. Deploy the
  new index and compatible consumers as one coordinated change; do not let a
  cached frontend client read a partially migrated local store.
- Keep the prefetch limit greater than or equal to the largest allowed final
  result count. A UI limit increase therefore requires checking the query
  adapter, not just changing a widget maximum.
- Display `hit.score` as an RRF/fused rank score or qualitative rank signal,
  never as cosine similarity or a percentage.
- Use `skill_url` for source links. Do not reconstruct or trust an arbitrary
  host from display text when the index already supplies a canonical HTTPS URL.
- A missing or zero-point collection is an onboarding state, not "no matches."
  Frontends should tell the user to run `clone_repos.py`, then
  `extract_search_raw.py`, then `index_qdrant.py`.

### Pull-request checklist

- [ ] Canonical constants are imported by every consumer; no duplicated model,
      vector, collection, or database-path literals were added.
- [ ] The payload table and frontend payload model match `load_skills()`.
- [ ] `query.py` and the frontend use the same models, named vectors, fusion,
      prefetch policy, filters, and default final limit.
- [ ] The correct incremental/full rebuild path was chosen, and no process held
      the embedded-store lock during indexing.
- [ ] CLI smoke search and frontend quality checks passed against the rebuilt or
      incrementally updated collection.
- [ ] Empty-index, empty-description fallback, score-label, result-limit, and
      GitHub-link behavior were exercised through the UI.
- [ ] If a scan-verdict field changed (`llm_scan`, `cli_security`,
      `locations[].vettd_scan_*`): its post-index producer, the payload table,
      `openapi.json`/`SkillHit`, `NEXTJS_INTEGRATION.md`, and the
      preservation list (`_PRESERVED_TOP_LEVEL_KEYS` /
      `_preserve_scan_publications`) were updated together — the producer is
      **not** `index_qdrant.py`.
- [ ] This document and user-facing setup/run instructions were updated.

## Current synchronization audit (2026-08-30)

Since the 2026-08-01 audit the **primary consumer changed**: the read-only
FastAPI query service (`app/query_service.py`, `openapi.json` v1.4.0) is now
the synchronized implementation of this contract — its `SkillHit` carries
every payload field in the table above (including `llm_scan` / `cli_security`),
it distinguishes a missing/empty collection via `_index_ready`, and it adds
the `mcp_servers` collection behind `asset_type: "mcp"`. The **Streamlit app
(`app/streamlit_app.py`) still lags** and should not be treated as a reference
implementation.

Verified against the live server-mode collection on 2026-08-30: `agent_skills`
holds **62,329 points**, named vectors `dense` + `sparse`, and sampled
payloads carry every field in the table above (`llm_scan` and `cli_security`
present only on scanned points, as documented).

Query-service state (synchronized):

- [x] `SkillPayload` / `SearchResult` / `SkillHit` carry `owner`, `content`,
      `repo_url`, `skill_url`, `locations`, `language`, `agent_compatibility`,
      `llm_scan`, `cli_security` — full payload shape round-trips through
      `/query`.
- [x] One `QdrantClient` cached for the process lifetime
      (`search._get_client()` / `mcp_search._get_client()` module global) —
      the `st.cache_resource` TODO, solved at the library layer.
- [x] Missing/empty collection distinguished from a zero-match query
      (`query_service._index_ready()` → `index_ready: false`), per the
      "Empty or missing collection" guidance in `NEXTJS_INTEGRATION.md`.
- [x] `sources` backfilled — the live collection's sampled points all carry a
      populated `sources` list.

Streamlit app (`app/streamlit_app.py`) — still open:

- [ ] Add a result-count control (still hardcoded to `search_skills`'s
      `limit=12` default; no widget).
- [ ] Replace the results `st.dataframe` with the documented card/expander
      view and label `hit.score` as a relative RRF rank signal.
- [ ] Distinguish a missing/empty collection from a valid zero-match query
      in the UI (the query *service* does; the Streamlit app still shows a
      generic error / "no matching skills").
- [ ] The randomized `Security_Scan` column is now labelled a placeholder in
      its `column_config` help text but is still rendered — remove it, or
      replace it with the real `llm_scan` / `cli_security` / `vettd_scan_*`
      verdicts now on the payload.
- [ ] Surface `llm_scan` / `cli_security` in the UI at all (currently
      dropped).
- [ ] Update frontend tests and README instructions to cover the synchronized
      behavior rather than the current dataframe.

Deliberate non-items:

- Duplicated constants in `app/search.py` / `app/mcp_search.py` (`COLLECTION`,
  `MODEL_NAME`, vector names): `app/` is a separately dependency-managed,
  separately Dockerized project whose build context does not include
  `../shared/` — the duplication is a documented exception (see
  `app/mcp_search.py`'s and `shared/qdrant.py`'s docstrings), not drift to
  fix. The values must still be kept identical by hand.

## Reference implementation

`query.py` is the minimal reference CLI — read it end to end before wiring
a new UI; it's ~40 lines and covers everything above.
