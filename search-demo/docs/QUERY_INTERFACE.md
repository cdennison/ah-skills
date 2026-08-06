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

| Field | Source | Notes |
|---|---|---|
| `id` | `uuid5(path)` | stable point id (Qdrant requires int or UUID), used to upsert/delete on re-index without disturbing unrelated points |
| `path` | file path relative to `search-raw/`, e.g. `owner/repo/skills/foo/SKILL.md` | stable identifier, also usable as a GitHub-style breadcrumb |
| `owner` | `rel.parts[0]` | GitHub org/user that owns the repo |
| `repo` | `rel.parts[1]` | one repo can contribute many skills |
| `repo_url` | `https://github.com/{owner}/{repo}` | link to the repo itself |
| `skill_url` | `https://github.com/{owner}/{repo}/blob/HEAD/{subpath}` | direct link to the `SKILL.md` (or extra README) on GitHub; `blob/HEAD` resolves to whichever branch is currently default, so it survives a `main`/`master` rename |
| `name` | frontmatter `name:`, falls back to the parent directory name | short slug/title |
| `description` | frontmatter `description:` | **plain-text only** (see below), empty string if the file has no description |
| `sources` | `registry.source_types()` for this skill's `owner/repo` in `repo-seeds/registry.json` | sorted list of discovery channels (`seed`/`search`/`manual`/`marketplace`) that surfaced the repo; empty list if the repo isn't in the registry (shouldn't normally happen) — see the caveat below |
| `content` | full raw file text, frontmatter included | used for embedding + full-text display/preview |
| `content_hash` | `sha1(content)` hex digest | lets `index_qdrant.py` detect unchanged files and skip re-embedding them |

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
   processes become a requirement, run Qdrant as a server instead of local mode.
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
- [ ] This document and user-facing setup/run instructions were updated.

## Current synchronization audit (2026-08-01)

The indexer and reference CLI have moved ahead of the current Streamlit
frontend. Before treating the frontend as an implementation of this contract,
sync these open items:

- [ ] Replace the duplicated constants in `app/search.py` with imports from the
      canonical source.
- [ ] Extend the frontend payload/result models to carry `owner`, `content`,
      `repo_url`, and `skill_url`; render a `content` snippet when `description`
      is empty and use `skill_url` for the source link.
- [ ] Cache one local `QdrantClient` with `st.cache_resource` instead of opening
      and closing the embedded store for every search.
- [ ] Add a result-count control whose default is 5, and pass that value to the
      outer `query_points(limit=...)` call while keeping prefetch limits large
      enough.
- [ ] Replace the results dataframe with the documented card/expander view and
      label `hit.score` as a relative RRF rank signal, not generic "Match."
- [ ] Distinguish a missing/empty collection from a valid zero-match query and
      show the rebuild sequence for the former.
- [ ] Remove the randomized `Security Scan` field or make its mock status
      unmistakable in the UI; it is not part of the Qdrant payload contract.
- [ ] Update frontend tests and README instructions to cover the synchronized
      behavior rather than the current 12-row dataframe.
- [x] Rebuild `qdrant_db/` after the parser/payload/URL changes. Verified on
      2026-08-01: the local collection contains 15,813 points and sampled
      payloads include all ten fields documented above.
- [x] Add `sources` (registry discovery channels) to `index_qdrant.py`'s
      payload, `app/search.py`'s `SkillPayload`/`SearchResult`, and render
      it as a "Discovered via" column in `app/streamlit_app.py`. 2026-08-03.
- [ ] `qdrant_db/` needs a **full rebuild** to backfill `sources` on
      existing points (see the staleness caveat above) — not yet run in
      this environment.

## Reference implementation

`query.py` is the minimal reference CLI — read it end to end before wiring
a new UI; it's ~40 lines and covers everything above.
