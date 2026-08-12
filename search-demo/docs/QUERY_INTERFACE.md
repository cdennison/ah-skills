# Qdrant query interface

How the `agent_skills` collection is shaped and how to query it — for anyone
wiring up a new frontend (Streamlit, etc.) on top of it, whether it's served
by a Docker Qdrant instance or the local embedded store at `qdrant_db/` (see
"Connection" below).

## Collection

- **Name**: `agent_skills`
- **Connection**: two supported modes, selected by environment variable —
  `index_qdrant.py`'s `get_client()` and `app/search.py`'s `_get_client()`
  both resolve the same way, so a caller only has to set these once, not
  per script:

  | Mode | How it's selected | Client construction |
  |---|---|---|
  | **Docker server** (default) | `SKILLS_QDRANT_DB_PATH` unset | `QdrantClient(url=SKILLS_QDRANT_URL)`, `SKILLS_QDRANT_URL` defaulting to `http://localhost:6333` — requires a Qdrant server already running there (e.g. `docker run -p 6333:6333 qdrant/qdrant`) |
  | **Local embedded** | `SKILLS_QDRANT_DB_PATH` set to a directory path (e.g. `qdrant_db`) | `QdrantClient(path=SKILLS_QDRANT_DB_PATH)` — on-disk, in-process, no server; takes an exclusive lock on that path for the life of the client (see the synchronization workflow below) |

  `SKILLS_QDRANT_DB_PATH` wins if both are set. Both env vars are read at
  import time in each script (`index_qdrant.py`, `app/search.py`), so set
  them in the environment before invoking `index_qdrant.py`, `query.py`, or
  `streamlit run app/streamlit_app.py` — there's no CLI flag for this, and
  every consumer must agree on the same mode/path/URL against a given
  collection or they'll silently read/write two different stores.
  Historically this repo only supported the local embedded store at
  `qdrant_db/`; if you see that path referenced elsewhere without an env var
  override, it's the embedded-mode default location, not evidence that
  embedded mode is still the only option.
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
| `ranking` | `_ranking_string()` over `repo-seeds/registry.json` | flattened `key=value` ranking/popularity string — see "Ranking metadata" below |
| `search_rank_<source>_<metric>` (dynamic, zero or more) | `_search_rank_fields()` over the same registry data | real top-level **int** fields, one per (query, sort) combo the repo's search ranking data covers — see "Ranking metadata" below; not present at all for a repo with no search-rank data for that metric |

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

## Ranking metadata (`ranking` string + derived columns)

Several sources contribute discovery-time ranking signal for a skill: GitHub
code search (`search`), skills.sh, and the marketplace/registry crawls. All of
it lands in a single free-text payload/CSV field, `ranking`, as space-separated
`key=value` tokens, e.g.:

```
skills_sh_rank=0 skills_sh_skill_count=1 skills_sh_top_installs=2813097 search_rank_agent_skills_best_match=12 search_rank_claude_skills_stars=44
```

**`export_csv.py` splits every `search_rank_<source>_<metric>=N` token out of
that string into its own CSV column** (`search_rank_columns()` /
`extract_search_ranks()`), so the flat CSV has, alongside `ranking`:

| Column | Meaning |
|---|---|
| `search_rank_agent_skills_best_match` | rank position when GitHub code-search results for the `agent-skills` query are sorted by best-match |
| `search_rank_agent_skills_stars` | rank position for the same query sorted by stars |
| `search_rank_claude_skills_best_match` | rank position for the `claude-skills` query, best-match sort |
| `search_rank_claude_skills_stars` | rank position for the `claude-skills` query, stars sort |
| `search_rank_codex_skills_best_match` | rank position for the `codex-skills` query, best-match sort |
| `search_rank_codex_skills_stars` | rank position for the `codex-skills` query, stars sort |

Lower is better (rank `0` = top result). These columns are **not hardcoded**:
`search_rank_columns()` derives the set from whatever `search_rank_*` tokens
actually appear in the data, so a new (query, sort) combo shows up
automatically on the next export.

`ranking` may also carry non-rank tokens that aren't split into columns:
`skills_sh_rank`, `skills_sh_skill_count`, `skills_sh_top_installs`.

### ⚠️ Legacy ambiguous `search_rank=N` token (fixed)

Older rows briefly carried a bare `search_rank=N` token with no source
suffix, written before per-(query, sort) attribution existed. It collided all
search sources into one field and couldn't be safely mapped to any of the
`search_rank_<source>_<metric>` columns above — `export_csv.py` correctly
never extracted it into a column, and it has since been **stripped from the
`ranking` string** in `skills_export_top.csv` (the ambiguous token gave no
signal once the split columns exist and was misleading to read). If you spot
`search_rank=N` in a raw `ranking` value, treat it as a pre-fix artifact with
no reliable source attribution, not as a real column.

### Filtering by ranking metadata

Because the split columns are plain numeric CSV columns (or, for a Qdrant-only
consumer, tokens inside the `ranking` payload string), a Streamlit frontend can
expose arbitrary comparisons over them without special-casing each column.

**CSV/pandas-backed frontend** — load `skills_export_top.csv`, coerce the
`search_rank_*` and `stars` columns to numeric, and support a small query
grammar like `column op value` (e.g. `search_rank_agent_skills_best_match>50`,
`stars<100`, `search_rank_claude_skills_stars<=10`):

```python
import re
import pandas as pd

df = pd.read_csv("skills_export_top.csv")
rank_cols = [c for c in df.columns if c.startswith("search_rank_")]
for c in rank_cols + ["stars", "duplicate_count", "name_collision_count"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

_OPS = {">=": "ge", "<=": "le", "!=": "ne", ">": "gt", "<": "lt", "=": "eq"}
_EXPR_RE = re.compile(r"^\s*(\w+)\s*(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$")

def apply_filter_expr(df: pd.DataFrame, expr: str) -> pd.DataFrame:
    """Apply a single 'column op value' expression, e.g.
    'search_rank_agent_skills_best_match>50'. NaN (missing/unranked rows)
    never match, matching the intuition that an unranked skill fails any
    numeric ranking filter."""
    m = _EXPR_RE.match(expr)
    if not m:
        raise ValueError(f"unrecognized filter expression: {expr!r}")
    col, op, value = m.group(1), m.group(2), float(m.group(3))
    if col not in df.columns:
        raise ValueError(f"unknown column: {col!r}")
    series = df[col]
    return df[getattr(series, _OPS[op])(value) & series.notna()]

# multiple expressions AND together, e.g. a Streamlit multi-filter box:
# "search_rank_agent_skills_best_match>50" + "stars<1000"
for expr in user_supplied_expressions:
    df = apply_filter_expr(df, expr)
```

Expose this as a free-text Streamlit input (one expression per line, ANDed
together) plus a dropdown of `rank_cols` so users don't have to remember exact
column names.

**Qdrant-native frontend** (querying live off `qdrant_db/` instead of the
CSV) — `index_qdrant.py`'s `load_skills()`/`refresh_metadata()`/
`prune_stale_locations()` write every `search_rank_<source>_<metric>` as a
real **top-level int payload field** (via `_search_rank_fields()`), alongside
the flattened `ranking` string — not just packed inside `ranking`. That means
these are filterable with a native `models.Filter`/`models.Range`, applied by
Qdrant as part of the ANN search itself (via `Prefetch(filter=...)` /
`query_filter=...`) rather than discarded from an already-fetched result set
afterward. This is exactly what `app/search.py`'s `filters_to_qdrant_filter()`
does:

```python
from qdrant_client import models

# search_rank_agent_skills_best_match <= 50 ("ranked 50 or better")
rank_filter = models.Filter(
    must=[
        models.FieldCondition(
            key="search_rank_agent_skills_best_match",
            range=models.Range(lte=50),
        ),
    ]
)
```

A point with no data for a given metric simply has no such payload key, so a
`Range` filter on it never matches — same "missing never matches" semantics
the CSV/pandas helper above uses. `app/search.py`'s `discover_rank_metrics()`
lists which `search_rank_*` fields actually exist in the collection right now
(reads real payload keys, not the `ranking` string) so a UI can build filter
widgets without hardcoding the known (query, sort) combos.

Because `client.set_payload()` **merges** rather than replaces, a metric that
disappears from `registry.json` (a repo drops out of a search ranking) would
otherwise leave a stale, silently-wrong field behind —
`index_qdrant.py`'s `_replace_search_rank_fields()` explicitly
`delete_payload()`s keys no longer present before setting the current ones;
any future write path that patches `search_rank_*` fields via `set_payload`
must do the same.

## Querying

Search is **hybrid**: dense (semantic) + sparse (BM25 keyword), fused with
Reciprocal Rank Fusion (RRF). `app/search.py`'s `search_skills()` is the
**single implementation** of this query — `query.py` (CLI) and
`app/streamlit_app.py` (UI) both call it directly rather than each carrying
their own copy of the query shape:

```python
# app/search.py
qdrant_filter = filters_to_qdrant_filter(filters)  # min_stars, sources, and rank_filters -- all native
results = client.query_points(
    collection_name=COLLECTION,
    prefetch=[
        models.Prefetch(
            query=models.Document(text=normalized_query, model=MODEL_NAME),
            using=DENSE_VECTOR_NAME,
            limit=max(limit, 20),
            filter=qdrant_filter,
        ),
        models.Prefetch(
            query=models.Document(text=normalized_query, model=SPARSE_MODEL_NAME),
            using=SPARSE_VECTOR_NAME,
            limit=max(limit, 20),
            filter=qdrant_filter,
        ),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    query_filter=qdrant_filter,
    limit=limit,
)
```

Notes:

- `models.Document(text=..., model=...)` triggers FastEmbed to embed the
  query text automatically, on the fly, with the same models used at index
  time — no manual embedding step, no API key.
- The prefetch limit (`max(limit, 20)`) is the candidate pool each sub-query
  pulls before fusion; `limit` on the outer `query_points` call is the final
  number of fused results returned. Keep prefetch limit ≥ final limit.
- `hit.score` after RRF fusion is **not** a 0–1 cosine similarity — it's a
  fused rank score. Don't render it as a percentage; use it only for
  relative ordering, or bucket it into rough tiers if you want a
  qualitative label.
- `qdrant_filter` (built by `filters_to_qdrant_filter()`) is passed into
  **both** `Prefetch.filter` and the outer `query_filter` — all of
  `min_stars`, `sources`, and `rank_filters` are native Qdrant
  `FieldCondition`s, so Qdrant applies them as part of the ANN search itself.
  There is no overfetch-then-discard step: `limit` is the actual number of
  points Qdrant returns, already matching every filter.
- `browse_skills()` (blank-query, filter-only browsing) passes the same
  `qdrant_filter` into `client.scroll(scroll_filter=...)` instead, since
  there's no query to run a `Prefetch`/fusion search against.
- `app/search.py` shares one lazily-created `QdrantClient` across every call
  in the process (`_get_client()`) rather than opening/closing one per query
  — opening a client against this collection costs upwards of 100s once it's
  past ~100k points, almost entirely in the open itself, not per-query work.

## Keeping the index and frontends in sync

This document is the contract between the index writer (`index_qdrant.py`)
and `app/search.py`, the **single query implementation** both `query.py`
(CLI) and `app/streamlit_app.py` (UI) call into. A change is complete only
when the contract, producer, `app/search.py`, and tests agree.

### Sources of truth

- `index_qdrant.py` owns the collection name, storage path, vector names,
  embedding models, payload fields, point IDs, and incremental update rules.
- This document describes that interface for consumers. Update it in the same
  change as the indexer; never document a planned schema as if it already exists.
- `app/search.py`'s `search_skills()`/`browse_skills()` are the only place
  hybrid-search semantics (prefetch limits, fusion, filters, default result
  count) are implemented. `query.py` imports and calls them rather than
  reimplementing the query — there is no second copy to keep in sync.
- `app/search.py` imports the canonical constants (`COLLECTION`, `DB_PATH`,
  vector names, model names) instead of copying their string values from
  `index_qdrant.py`'s literals.
- `app/search.py`'s `SkillPayload` must represent every field any consumer
  renders or uses for a fallback/link. Unknown additive fields may be
  ignored, but required fields must not silently disappear at the parsing
  boundary.

### Change-impact matrix

| Change | Update together | Index action |
|---|---|---|
| Raw files added, changed, or removed under `search-raw/` | source data only; confirm the existing payload/query contract still applies | incremental `index_qdrant.py` run |
| Frontmatter parser or derived payload fields change | `index_qdrant.py`, payload table above, frontend payload model/rendering, CLI output if relevant, tests | **full rebuild** |
| Payload field renamed, removed, or changes meaning | producer, this document, all consumers, fixtures/tests, migration/release notes | **full rebuild** and coordinated frontend release |
| Dense/sparse model, vector name, vector size, distance, or sparse modifier changes | canonical constants/config, this document, `app/search.py`, tests | **full rebuild** |
| Hybrid query shape, fusion method, prefetch size, filters, or result default changes | querying section above, `app/search.py` (only — `query.py` and the UI pick it up automatically), tests | no rebuild unless indexed vectors/payload also change |
| GitHub URL/path convention changes | payload derivation, payload table, frontend link rendering, tests | **full rebuild** because URLs are derived payload |
| UI-only layout or copy changes | frontend and frontend tests | no rebuild |

The indexer decides whether a point changed from the raw file's `content_hash`.
That catches source-file edits, but it does **not** detect changes to parser
logic, derived URLs, payload shape, embedding text construction, model choice,
or vector configuration when the raw file is unchanged. Until the index stores
and checks an explicit schema/index version, treat every such change as a full
rebuild rather than relying on the incremental path.

### Synchronization workflow

1. **In local embedded mode (`SKILLS_QDRANT_DB_PATH` set), stop every process
   using that path first.** Embedded Qdrant takes an exclusive path lock, so
   `index_qdrant.py` will fail to open the store if Streamlit or `query.py`
   already hold it — stop Streamlit before indexing or rebuilding, and don't
   run `query.py` concurrently with the app. **In Docker-server mode
   (`SKILLS_QDRANT_DB_PATH` unset)** there is no client-side path lock —
   multiple processes can hold connections concurrently — but re-indexing
   still writes into the same collection a running frontend is reading from,
   so avoid a full rebuild (which drops and recreates the collection) while
   the app is serving live traffic; an incremental run is safe to do
   concurrently since it only upserts/deletes individual points.
2. **Update the contract and producer together.** Change `index_qdrant.py` and
   the collection/payload/query sections in this document in the same review.
3. **Update `app/search.py`.** Since `query.py` and the frontend both call
   into it directly, changing it there is sufficient for query shape; still
   check payload types, result-limit controls, fallbacks, links, labels, and
   empty collection behavior in each consumer's rendering layer.
4. **Choose incremental or full indexing using the matrix above.** For a full
   rebuild, preserve the old store until verification succeeds — the backup
   mechanics differ by connection mode:

   **Local embedded** (`SKILLS_QDRANT_DB_PATH` set) — move the on-disk
   directory aside:

   ```bash
   mv qdrant_db qdrant_db.pre-interface-change
   uv run python index_qdrant.py
   ```

   Restore the backup if verification fails. Remove it only after both CLI and
   frontend checks pass.

   **Docker server** (`SKILLS_QDRANT_DB_PATH` unset) — there's no directory to
   move; use a Qdrant snapshot instead so the old collection state is
   recoverable without a re-embed:

   ```bash
   curl -X POST "http://localhost:6333/collections/agent_skills/snapshots"
   uv run python index_qdrant.py
   ```

   If verification fails, delete the (now-rebuilt) `agent_skills` collection
   and recover it from the snapshot recorded above before retrying — see
   Qdrant's snapshot-restore docs. Either way, point every consumer at the
   same `SKILLS_QDRANT_URL`/`SKILLS_QDRANT_DB_PATH` during verification so
   you're not comparing against a stale second store.
5. **Verify the stored collection before starting Streamlit.** Confirm the
   collection exists, contains points, exposes the expected named vectors, and
   that sampled payloads contain every required field with the documented
   meanings.
6. **Run the reference CLI and frontend checks:**

   ```bash
   uv run python query.py "excel spreadsheets" -n 5
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
- Every consumer process (`index_qdrant.py`, `query.py`,
  `app/streamlit_app.py`, ad hoc scripts) must be started with the same
  `SKILLS_QDRANT_DB_PATH`/`SKILLS_QDRANT_URL` environment to read/write the
  same collection — there is no default that reconciles a mismatch, they are
  two independent stores.

### Pull-request checklist

- [ ] Canonical constants are imported by every consumer; no duplicated model,
      vector, collection, or database-path literals were added.
- [ ] The payload table and frontend payload model match `load_skills()`.
- [ ] `query.py` still calls `app/search.py` directly (no reimplemented query
      logic); models, named vectors, fusion, prefetch policy, filters, and
      default final limit all come from that one place.
- [ ] The correct incremental/full rebuild path was chosen, and no process held
      the embedded-store lock during indexing.
- [ ] CLI smoke search and frontend quality checks passed against the rebuilt or
      incrementally updated collection.
- [ ] Empty-index, empty-description fallback, score-label, result-limit, and
      GitHub-link behavior were exercised through the UI.
- [ ] This document and user-facing setup/run instructions were updated.
- [ ] If connection-mode handling changed, both Docker-server and local
      embedded paths were exercised, not just whichever mode is default.

## Current synchronization audit (2026-08-01)

The indexer and reference CLI have moved ahead of the current Streamlit
frontend. Before treating the frontend as an implementation of this contract,
sync these open items:

- [ ] Replace the duplicated constants in `app/search.py` with imports from the
      canonical source.
- [ ] Extend the frontend payload/result models to carry `owner`, `content`,
      `repo_url`, and `skill_url`; render a `content` snippet when `description`
      is empty and use `skill_url` for the source link.
- [x] Cache one local `QdrantClient` instead of opening and closing the
      embedded store for every search — `app/search.py`'s `_get_client()`
      lazily opens one client and reuses it for the life of the process.
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

`app/search.py`'s `search_skills()` / `browse_skills()` are the actual
reference implementation — read them end to end before wiring a new
consumer. `query.py` is a ~30-line CLI wrapper around `search_skills()`;
read it for the minimal integration pattern (add `app/` to `sys.path`,
import, call, render).
