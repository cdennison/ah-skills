# MCP pipeline: end-to-end architecture and rate limits

One-page reference for how a row gets from an upstream source to a
queryable Qdrant point, and exactly what's rate-limiting each hop. The
narrative history of *why* each piece exists is in `MCP_PIPELINE.md` and
`PROPOSED_PIPELINE.md` -- this doc is the current-state map, kept in sync
as scripts change, not a chronology.

## Pipeline stages

```
                          ┌─────────────────────────┐
                          │  mcp-repo-seeds/         │
                          │  registry.json           │  <- single source of truth,
                          │  (mcp_registry.py)        │     one row per unique server
                          └─────────────────────────┘
                                    ▲
        ┌───────────────┬──────────┼──────────┬──────────────────┬─────────────────┐
        │               │          │          │                  │                 │
 pull_official_    pull_glama.py  pull_seed_  download_    fetch_mcp_       fetch_mcp_
 registry.py                      repo.py     readmes.py   rankings.py      security.py
        │               │          │          │                  │                 │
   official MCP    Glama's MCP  awesome-mcp-  README.md     GitHub stars/    OSV.dev vuln
   registry API     directory   servers seed  per repo,     language, npm    scan (+ direct
                       API         list        3-tier        downloads/       deps, 1 level)
                                  (scan_mcp.py               score
                                   per repo)
```

Enrichment order matters: `pull_*`/`download_readmes.py`/
`classify_mcp_registry.py` establish the row and its readme/category first;
`fetch_mcp_rankings.py`/`fetch_mcp_security.py` layer ranking/security data
on top of an already-identified row (both need `repo_url`/
`package_identifier` to already be resolved). None of these should ever run
concurrently with each other -- see "Concurrency" below.

```
                          ┌─────────────────────────┐
                          │  mcp-repo-seeds/         │
                          │  registry.json           │
                          └─────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
             export_mcp_csv.py  mcp_stats.py   index_qdrant.py
                    │               │               │
             mcp_servers_      console        Qdrant collection
             export.csv        snapshot       "mcp_servers"
             (human review)    (coverage %s)   (dense+sparse
                                                 vectors + full
                                                 payload)
                                                       │
                                                       ▼
                                          app/mcp_search.py (query_service)
                                          -- read-only, hybrid search
```

`index_qdrant.py` is the only writer of the `mcp_servers` collection.
`--rankings-only` pushes payload-only updates (stars/downloads/security/
language/descriptions) onto already-indexed points without re-embedding;
`--rebuild`/`--sample-ranked`/`--ids` are the reviewed, reusable paths for
wiping/rebuilding or targeting a specific subset (see that script's own
docstring -- never do either via an ad hoc client call, confirmed the hard
way why that matters).

## Rate limits, by external service

All HTTP fetching goes through `shared/http.py`'s `RateLimiter`
(`shared/rate_limit.py`) via two preconfigured instances. Every window is
enforced *simultaneously*, not as alternatives -- e.g. `default_limiter()`
never exceeds 10/s **and** never exceeds 100/min **and** never exceeds
10000/hr, all three at once.

| Limiter | Limits | Used for |
|---|---|---|
| `default_limiter()` | 10/s, 100/min, 10000/hr | registry.modelcontextprotocol.io (`pull_official_registry.py`), glama.ai API (`pull_glama.py`), registry.npmjs.org + api.npmjs.org (`fetch_mcp_rankings.py`'s downloads phase, `fetch_mcp_security.py`'s npm dependency-manifest fetch), pypi.org (`fetch_mcp_security.py`'s PyPI dependency-manifest fetch), **api.osv.dev** (`fetch_mcp_security.py`'s vuln scan -- both the top-level and per-dependency queries) |
| `github_limiter()` | 10/s, 4000/hr | api.github.com, raw.githubusercontent.com, codeload.github.com -- `pull_seed_repo.py` (per-repo `server.json`/`package.json` scan), `download_readmes.py` (all 3 tiers: raw, API, clone), `fetch_mcp_rankings.py`'s stars phase (`GET /repos/{owner}/{repo}`, also captures `language` for free off the same call) |

**Why 4000/hr for GitHub, not GitHub's real 5000/hr authenticated
ceiling**: deliberately kept below the real quota so this pipeline is never
the one tripping it -- confirmed via `shared/github_auth.py`'s
`rate_limit_status()`. GITHUB_PAT (from `.env`/`.env.local`, via
`shared/github_auth.py`) is attached automatically to requests against
those three GitHub hosts only; every other host above is hit
unauthenticated, on purpose (a GitHub token has no business going to
npm/PyPI/OSV/Glama/the official registry).

**api.osv.dev specifically** (the service most recently added, and the one
explicitly asked to be tracked here): no documented hard rate limit from
OSV, but paced at the same conservative `default_limiter()` as everything
else non-GitHub -- 10/s and 100/min simultaneously. `fetch_mcp_security.py`
issues 1 query per package scanned, plus (since the direct-dependency pass
was added) 1 manifest fetch + 1 extra OSV query per direct dependency --
see that script's "DEPENDENCY COVERAGE" docstring section for what that
does and doesn't cover (direct deps only, no transitive resolution).

### Retry/backoff behavior (applies to every host above, via `shared/http.py`)

- **429** (standard rate-limit-exceeded): sleep `RATE_LIMIT_SLEEP_SECONDS`
  (70 min) and retry -- does not count against `max_retries`.
- **GitHub 403 with `X-RateLimit-Remaining: 0`**: treated exactly like 429
  (sleep 70 min, retry). GitHub's primary rate limit responds 403, not 429,
  once core quota hits zero -- confirmed live; a run against
  api.github.com burned through ~2600 doomed requests with zero backoff
  before this was fixed. Any *other* 403 (a private/blocked repo, e.g.) is
  re-raised immediately -- sleeping on a real permissions error would just
  waste an hour to fail the same way again.
- **5xx / connection errors**: short exponential backoff
  (`min(2**attempts * 2, 30)` seconds), capped at `max_retries=4`.
- **404**: never retried, surfaced immediately to the caller (via
  `get_text_or_none()` for "absent is a normal outcome" cases, or as a
  raised `HTTPError` everywhere else).

### Non-rate-limit caps worth knowing about here

- **`MAX_UPSERT_BYTES`** (`shared/qdrant.py`, 24MB): Qdrant upsert batches
  are sub-split by actual serialized point size, not a fixed point count --
  a fixed-count batch was once observed to produce a 273MB request.
- **`MAX_DIRECT_DEPS_SCANNED`** (`fetch_mcp_security.py`, 40): sanity cap
  on the dependency-tree pass, not a real-world limiter -- guards against a
  malformed manifest turning one row into dozens of extra OSV calls.
- **`README_DESCRIPTION_MAX_CHARS`** (`mcp_registry.py`, 600, enforced
  exactly -- including the `"..."` suffix, found and fixed an off-by-few
  bug here): caps `extract_readme_description()`'s output so a pathological
  README can't blow up registry.json/Qdrant payload size.

## Concurrency: registry.json has exactly one safe writer at a time

Every pull/enrichment script does a full read-modify-write cycle:
`mcp_registry.load_registry()` (whole file into memory) ->
mutate rows -> `mcp_registry.save_registry()` (whole file back out,
periodically during a long run, via `SAVE_EVERY`-row checkpoints). Two of
these running concurrently silently clobber each other -- whichever
finishes its next periodic save last wins, discarding the other's
in-between progress. Confirmed painfully in practice, twice:

1. A targeted `--ids` fetch's writes got silently overwritten by a
   concurrently-running full `fetch_mcp_rankings.py` background job's next
   periodic save (both held their own stale in-memory copy of the row).
2. `save_registry()`'s old non-atomic `write_text()` (truncates the file
   before writing) plus a `supervise.sh stop`-sent SIGTERM landing in that
   exact window left `registry.json` completely empty once -- 82K rows,
   gone in the time it takes a signal to arrive. Fixed at the write level
   (`save_registry()` now writes to a temp file and `os.replace()`s it
   atomically -- a kill can no longer produce a *truncated* file, though it
   can still lose whatever wasn't saved yet). Recovery path for that
   specific failure mode: `rebuild_registry_from_raw.py` (replays the
   still-cached raw pull dumps through the real `upsert_entry()`
   functions -- no network, no data reconstruction guesswork).

**The remaining, NOT-yet-fixed gap** is the lost-update race itself (item
1) -- atomic writes prevent *corruption*, not *silently discarding a
concurrent writer's progress*. Until that has a real fix (file locking, or
save-time read-merge-write), the operating rule is procedural, not
enforced by code: **never run two of `pull_official_registry.py` /
`pull_glama.py` / `pull_seed_repo.py` / `download_readmes.py` /
`classify_mcp_registry.py` / `fetch_mcp_rankings.py` / `fetch_mcp_security.py`
at the same time.** `run_overnight.sh` sequences all of them for exactly
this reason; `supervise.sh` (used for the two long-running fetch_mcp_*.py
passes) makes it easy to check `status`/`stop` a job before starting
another one that touches the same file.

## Files referenced here

- `mcp_registry.py` -- the schema, the atomic save, `extract_readme_description()`
- `shared/http.py` / `shared/rate_limit.py` / `shared/github_auth.py` -- the limiters/retry/auth described above
- `shared/qdrant.py` -- Qdrant client construction, embedding models, size-capped upsert
- `supervise.sh` -- generic retry-on-crash wrapper for long-running scripts (`start`/`status`/`stop`)
- `run_overnight.sh` -- the full sequenced pipeline run
- `rebuild_registry_from_raw.py` -- registry.json recovery from cached raw dumps
- `test_e2e_pipeline.py` -- live, networked regression test against one real row, covering every stage above
