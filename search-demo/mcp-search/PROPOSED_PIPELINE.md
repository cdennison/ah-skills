# Proposed: a recurring MCP server discovery pipeline

This is a proposal, not yet implemented, for turning the `mcp-search/`
prototype (see [`MCP_PIPELINE.md`](MCP_PIPELINE.md) for what's built and
proven so far) into a recurring pipeline analogous to the main project's
[`../docs/DAILY_JOB.md`](../docs/DAILY_JOB.md) -- same spirit (numbered steps,
additive-only source tracking, explicit staleness handling), scoped to MCP
servers instead of Claude skills.

## Side by side with the skills pipeline -- and why this stays fully separate
The two pipelines share a *shape* (registry → clone/scan → extract →
index → export), which is exactly why it's worth being explicit that they
should not share *state*. Mapping the skills pipeline's pieces to their
proposed MCP equivalents:

| Concern | Skills pipeline (existing) | MCP pipeline (proposed) |
|---|---|---|
| Source-of-truth registry | `repo-seeds/registry.json` | separate file, e.g. `mcp-repo-seeds/registry.json` -- different schema entirely (server.json-shaped fields, not `SKILL.md` fields) |
| Seed list (awesome-*) | `repo-seeds/awesome-agent-skills/` (vendored from `VoltAgent/awesome-agent-skills`) | `mcp-repo-seeds/awesome-mcp-servers/` (vendored from `punkpeye/awesome-mcp-servers`, source 6 above) |
| Clone destination | `repos/` | `mcp-repos/` -- own directory, own `.clone_state.json`-equivalent |
| Extracted content | `search-raw/` (`SKILL.md` + paired README) | `mcp-search-raw/` (`server.json` + `package.json` + README) |
| Vector index collection | `agent_skills` | `mcp_servers` -- a distinct collection name, not a shared one |
| Vector index storage | `qdrant_db/` (local embedded) or a server via `SKILLS_QDRANT_URL` | its own path/collection -- see below, this is the one that actually matters |
| Env var namespace | `SKILLS_QDRANT_URL`, `SKILLS_QDRANT_DB_PATH` | new names, e.g. `MCP_QDRANT_URL`, `MCP_QDRANT_DB_PATH` -- never reuse the skills ones even by accident |
| CSV export | `skills_export.csv`, `skills_export_top.csv` | `mcp_servers_export.csv` (the prototype's `npm_mcp_candidates.csv`, generalized once multi-source) |
| Ranking source | GitHub stars + skills.sh leaderboard | GitHub stars + npm/PyPI downloads (this doc's ranking section) |
| Recurring-job doc | `../DAILY_JOB.md` | this doc, eventually promoted to its own `MCP_DAILY_JOB.md` once implemented |

**One structural difference worth calling out, not just a naming
parallel**: the skills pipeline's `batch_pipeline.py` clones every repo in
bounded batches specifically because extracting `SKILL.md` files needs
the full repo checked out on disk. The MCP pipeline's `scan_github_repo`
(source 5's no-clone table) needs only `server.json`/`package.json`/
`README.md` per repo, fetched individually over HTTP -- so it structurally
doesn't need the disk-space-checking, batch-then-clean machinery
`batch_pipeline.py` had to build. That's a reason to keep the pipelines
separate at the *code* level too, not just data: porting this one's logic
into `batch_pipeline.py` would import a clone-everything assumption this
pipeline is specifically designed to avoid.

**Why separate storage isn't just tidiness -- it avoids a documented failure
mode.** `../DAILY_JOB.md`'s own "non-obvious issues" section flags: *"`qdrant_db/`
is a local embedded store, not a server -- it does not support concurrent
writers... the second one crashes with `RuntimeError: Storage folder ...
is already accessed by another instance of Qdrant client`."* Two pipelines
writing to the same embedded store, even to different collections, would
either hit that crash directly (if both use embedded-file mode) or force
awkward run-schedule coordination between two otherwise-independent cron
jobs to avoid overlap. A separate path (or, if a Qdrant *server* is in use
rather than the embedded mode, at minimum a separate collection with
`mcp_`-prefixed points and no shared indexing code) sidesteps this
structurally rather than by careful scheduling. Separate storage also
keeps blast radius contained -- a bad re-index or a corrupted extraction in
one pipeline can't touch the other's data -- and keeps payload schemas
honest (a skill's fields and a server's fields are genuinely different
shapes; forcing them into one collection would mean either a lossy shared
schema or a lot of nullable fields on both sides). Nothing stops a search
*application* from querying both collections and merging results at the
UI layer later -- that's a reuse decision made above the storage layer, not
one that requires shared storage underneath it.

## Sources, ranked by how much they save you

### 0. The official MCP registry -- highest priority of all
While building the prototype, found that `registry.modelcontextprotocol.io`
is a live, first-party, paginated API:

```bash
curl "https://registry.modelcontextprotocol.io/v0/servers?limit=100&cursor=<nextCursor>"
```

Each entry is **already shaped like the `server.json` we've been manually
extracting from repos** -- name, description, version, `packages[]`
(registryType/identifier/transport/environmentVariables) or `remotes[]`,
plus `_meta` publish/update timestamps and status. This is the closest
thing to ground truth available: servers that publish here have already
done the structured self-description work `scan_mcp.py` currently
reverse-engineers from READMEs and package registries.

**Implication for the whole pipeline**: this should be the primary daily
pull, not npm/PyPI search. npm/PyPI search exists to catch what *hasn't*
made it into the official registry yet -- which, going by the prototype's
100-npm-result sample, is still most of the ecosystem. Treat the official
registry as "confirmed, structured" and npm/PyPI/GitHub search as
"candidates, need extraction."

### 1. Glama.ai -- a second structured, public API, arguably better than npm for this
`https://glama.ai/api/mcp/v1/servers` is a public, cursor-paginated JSON API
(no auth, no key) for a community-curated MCP directory. Confirmed via
direct requests -- `robots.txt` disallows crawling `/api/` generally but
this is a documented data API, not a page being scraped; it's the same
category of source as the official registry, not a scrape target:

```bash
curl "https://glama.ai/api/mcp/v1/servers?first=100&after=<endCursor>"
curl "https://glama.ai/api/mcp/v1/servers?query=mongodb"   # free-text filter
curl "https://glama.ai/api/mcp/v1/servers/<id>"            # single-server detail (deprecated, use /:namespace/:slug)
curl "https://glama.ai/api/mcp/v1/attributes"               # reference list of the 4 filterable attribute values
```

**No sort/rank parameter exists.** Confirmed against the published OpenAPI
spec (`https://glama.ai/api/mcp/openapi.json`) -- `GET /v1/servers` only
documents `after`, `first`, `query`. Empirically tried `sort=`, `sortBy=`,
and `attributes=` too; all three are silently ignored (identical results to
no param at all) -- the site's own sort/filter UI isn't backed by anything
exposed here. Default order is `createdAt` descending (decoded a page
cursor: `{"createdAt": 1786732485, "id": "..."}`) -- newest-added-to-Glama
first, not popularity. **The response schema has no stars/score/download
field at all**, so Glama cannot supply ranking data by itself -- pull it
fully via pagination (there's nothing to sort by anyway, so exhaustive
pagination is the only mode) and get actual popularity from GitHub stars
(source 5, below) joined on `repository.url`, same as this pipeline already
needs to do regardless of Glama.

Each entry already has, **for free, exactly the columns this project's
proposal above says still need building**:
- `description` -- clean, human-written prose (not badge soup, not a
  monorepo-wide tagline -- e.g. compare Glama's description of a Univer-style
  package against that package's raw npm `description` field in the current
  CSV). This is effectively the "description extraction" problem already
  solved by Glama's own curation/review process.
- `environmentVariablesJsonSchema` -- structured config/auth requirements
  (`required[]`, per-field `description`), i.e. exactly the proposed
  `requires_auth`/`config_summary` columns, pre-computed.
- `attributes` -- includes `hosting:local-only` / `hosting:remote-capable` /
  `hosting:hybrid` and `author:official` (confirmed via a 50-row sample).
  This **is** the proposed "deployment mode" column, already computed by
  someone else, plus a bonus official/community-authorship signal this
  proposal hadn't planned for.
- `repository.url`, `tools[]` (actual declared tool list, when populated),
  `spdxLicense`.

**The glama.ai *website* additionally shows weekly npm downloads and GitHub
stars per server -- these are NOT in the public API response (confirmed:
not in any of the fields listed above), and, more importantly, should NOT
be trusted even when scraped from the page.** Verified this concretely
against `https://glama.ai/mcp/servers/broisnischal/openapi-mcp`:
- The weekly-download figure it shows is close to real (npm's own
  authoritative endpoint, `api.npmjs.org/downloads/point/last-week/
  openapi-mcp`, returns 32 for the same week) -- but "close" is the
  problem, not the reassurance. It's out of sync with the primary source
  by a small but real margin, presumably a caching lag.
- Worse: the API's own `repository.url` for this entry is
  `github.com/broisnischal/openapi-mcp` -- but that repo's actual
  `package.json` has `"name": "hono"`, not `openapi-mcp`. It is very
  unlikely to be the real source of the `openapi-mcp` npm package at all.
  The same page's embedded data additionally references a **third**,
  unrelated repo (`minamorl/openapi-mcp-bridge`) somewhere in its payload.
  Whatever star/download number is displayed is anchored to a repo
  association that's demonstrably not reliably correct for this entry --
  not just a stale-cache problem, a wrong-entity problem.

**Conclusion for the pipeline**: pull weekly downloads and GitHub stars for
every entry, as originally intended, but **from the authoritative sources
directly** (`api.npmjs.org/downloads/point/last-week/<pkg>` for npm,
GitHub's API for stars), keyed off `repository.url`/package identifiers
this pipeline already resolves itself -- never take glama.ai's displayed
download/star numbers as ground truth, and don't even trust its
`repository.url` blindly without the same cross-check `scan_mcp.py` already
does (fetch `package.json`/`server.json` from the claimed repo and verify
the name actually matches before accepting the link).

**Implication**: Glama should sit alongside the official MCP registry as a
tier-0 source for description/config/deployment-mode data, pulled before
npm/PyPI search, not after -- but it is not a ranking source, that still
has to come from GitHub stars regardless. It won't have every package npm
search would surface (community-submitted directory, review-gated), but
for whatever it does have, it's strictly richer descriptive data than what
this pipeline currently derives itself from npm+GitHub. Practical
approach: pull Glama fully (paginate `first`/`after` to exhaustion -- no
sort to pick, so "fully" is the only mode; saw `hasNextPage: true` past
the first page in the initial check, total count not exposed but the API
supports full pagination), match against the npm/PyPI/official-registry
dataset by `repository.url`, and **prefer Glama's
`description`/`attributes`/`environmentVariablesJsonSchema` over
self-derived versions wherever a match exists** -- falling back to this
project's own extraction/classification only for packages Glama doesn't
have.

### 2. mcpservers.org -- a large curated directory, but scrape-only (no API)
`https://mcpservers.org` ("Awesome MCP Servers") is server-rendered HTML
with **no public JSON API** -- `/api/` is explicitly disallowed in
`robots.txt` (internal Next.js routes like `/api/og`, not a data endpoint).
It does, however, expose a full sitemap:

```
https://mcpservers.org/sitemap.xml
  -> https://mcpservers.org/sitemaps/servers/{1..6}.xml   (~4,488 URLs each,
                                                             ~27k total)
```

Each entry is `https://mcpservers.org/servers/<owner>/<repo>` -- **the URL
slug is literally the GitHub owner/repo**, which is convenient for matching
against everything else this pipeline already tracks by repo url. The
detail pages are plain server-rendered HTML (confirmed -- no JS execution
needed, `curl` alone returns full text) containing description, GitHub
star count, source repo link, and the tool list, e.g. for
`modelcontextprotocol/fetch`:

> "Fetch MCP Server ... A Model Context Protocol server that provides web
> content fetching capabilities... Available Tools: fetch - Fetches a URL
> from the internet and extracts its contents as markdown..."

**Implication**: usable, but as an HTML-scrape source (needs an HTML
parser, not `json.load`), and lower priority than Glama/the official
registry precisely because there's no structured API -- 27k pages is a lot
of individual HTTP GETs to parse by hand, and unlike Glama's `attributes`
field, deployment-mode/auth signals would have to be re-derived from prose
same as this pipeline already does for npm. Value-add over what's already
built: it's a second independent star-count/popularity signal, and the
owner/repo-shaped URL makes it trivial to check "does mcpservers.org know
about this repo" as one more corroborating signal, worth pulling via the
sitemap once and diffing against the growing candidate set rather than
treating it as a primary extraction source.

### 3. npm search (built, proven -- `fetch_npm_mcp_candidates.py`)
`GET registry.npmjs.org/-/v1/search?text=mcp` → up to 250 hits/call, then
one `GET registry.npmjs.org/<package>` per hit for readme + full metadata.
Already rate-limited to ~9.2 req/min per the earlier requirement.

### 4. PyPI search -- same shape, different endpoint, not yet built
PyPI's search API was deprecated; the practical equivalent is
`https://pypi.org/simple/` (package name index only, no metadata) combined
with `https://pypi.org/pypi/<package>/json` per package for detail --
**or**, better, PyPI's search-via-XML-RPC is gone but
`https://pypi.org/search/?q=mcp` (the HTML search UI) has no public JSON
API, so the realistic approach is:
- Pull the full package name index once (`pypi.org/simple/`, ~30MB of
  names, no auth, cacheable), filter names containing "mcp" locally, then
  fetch `pypi.org/pypi/<name>/json` per match for description/readme/
  classifiers -- same per-package detail shape as the npm script, same
  rate-limiting discipline.
- `pypi.org/pypi/<name>/json`'s `info.description` is often the full
  README (PyPI renders whatever `long_description` the package sets), so
  this may not need the GitHub/tarball backfill tiers `backfill_readmes.py`
  needed for npm.
- **Not yet built.** Proposing this as the shape; would want to prototype
  against ~10 known Python MCP servers (e.g. anything under
  `modelcontextprotocol/python-sdk`'s example servers) before trusting it
  at scale, same as the description-extraction proposal below.

### 5. GitHub search -- ranking signal only, clone as an absolute last resort
GitHub search (`search_github.py`-equivalent, per the main pipeline's
pattern) is valuable here for exactly one thing: **surfacing servers that
exist only as a GitHub repo with no package-registry presence at all** --
which, per the Atlassian investigation below, is a real and important
category, not a corner case.

**Ranking data -- stars and downloads for every entry -- should come from
here and npm/PyPI directly, not from a third-party directory's displayed
numbers** (see the Glama caveat above: its website shows both, but they're
tied to a `repository.url` that isn't reliably correct, so the numbers
inherit that unreliability). Concretely, per candidate:
- **GitHub stars**: `GET api.github.com/repos/<owner>/<repo>` ->
  `stargazers_count` (also gets `pushed_at` for an activity-recency
  signal). One call per unique repo; batchable via GitHub's GraphQL API if
  volume warrants it later.
- **npm weekly downloads**: `GET api.npmjs.org/downloads/point/last-week/
  <package>` -- a separate, dedicated endpoint from the search API's
  `downloads.weekly` field used during discovery; this one is the
  authoritative point-in-time figure, no search-index caching lag.
- **PyPI downloads**: no first-party equivalent: PyPI does not track
  download counts itself. The de facto standard is the third-party
  `pypistats.org` API (`GET pypistats.org/api/packages/<package>/recent`)
  -- itself a secondary/community source (same trust caveat as Glama in
  principle), but there's no first-party alternative to fall back to for
  PyPI specifically. Worth periodically checking its numbers against
  something independent if it starts being load-bearing for ranking, same
  lesson as the Glama finding above.

**Cloning should be the exception, not the rule, and this needs to be
explicit in the pipeline, not just a preference:**

| Situation | Action |
|---|---|
| Package exists on npm or PyPI | Use the registry API + `scan_github_repo` (raw.githubusercontent.com, no clone) for `server.json`/`package.json`/`README.md`. **Never clone.** |
| Package exists only on the official MCP registry, repo is public on GitHub | `scan_github_repo`, no clone. |
| No package registry entry, repo is public on GitHub | `scan_github_repo` first (covers the common case -- most repos have `server.json` or `package.json` readable via raw content). Only clone if that's insufficient (e.g. need to inspect actual source, not just manifests -- multi-file transport config, dynamically generated server.json, etc.). |
| Repo is private, or `raw.githubusercontent.com` 404s on everything expected | Clone, as a last resort. |

**Why this matters enough to call out explicitly**: a git clone is slow
(seconds to tens of seconds per repo, network- and disk-bound), and at
npm-search-result volumes (100s-1000s of packages/day) that adds up to a
meaningful chunk of pipeline runtime for something `raw.githubusercontent.com`
answers in milliseconds per file with zero disk footprint. The main
project's own `batch_pipeline.py` had to build batching + disk-space
checks + cleanup specifically because full clones at scale fill disk --
this pipeline should avoid ever needing that machinery by defaulting to
the no-clone path (`scan_github_repo`, already built and tested) and
treating an actual `git clone` as a deliberately logged, rare fallback,
not silent default behavior.

### 6. Seed repos ("awesome lists") -- same vendoring pattern as the main pipeline
The main skills pipeline already has this exact mechanism for
`officialskills.sh`: `repo-seeds/repo_seeds.json` tracks an
`upstream_repo` + `vendored_path` + `last_pulled`, `refresh_seeds.py`
re-clones upstream and overwrites the vendored copy, `registry.py
sync-seed` regex-scrapes `github.com/...` links out of *that vendored
copy* into the registry (see `../DAILY_JOB.md` step 3's staleness
discussion -- vendoring is what makes "re-run sync-seed" and "the source
list is actually fresh" two different questions, and both need answering).

**`punkpeye/awesome-mcp-servers`** is the direct MCP-world equivalent of
`VoltAgent/awesome-agent-skills`, and should be seeded the same way. Pulled
and inspected it directly:

```bash
curl "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/HEAD/README.md"
```

~3,800 lines, curated by category (Aggregators, Browser Automation, etc.),
with a legend marking 🎖️ official implementations and language/scope/OS
icons per entry -- richer than a bare link list, worth parsing beyond just
the regex-extract-github-urls approach `build_registry.py` uses today, if
the icons prove reliably present.

**Important, non-obvious overlap to know going in**: this repo's own
README states it *"is synced with"* `glama.ai/mcp/servers` -- i.e. this
seed list and source 1 (Glama) are, to a significant degree, the same
underlying curation surfaced two ways (one as a maintained README, one as
Glama's structured API/directory). Don't treat a seed-repo hit and a Glama
hit as two independent confirmations of the same server; they likely share
a common source. The seed repo is still worth pulling -- it may have
long-tail entries not yet synced to Glama, and gives an independent
"someone curated this" signal distinct from raw npm/PyPI search noise --
but budget for high overlap with source 1, not two disjoint datasets.
Same vendoring/staleness handling as `officialskills.sh`:
`refresh_seeds.py`-equivalent re-clones on a schedule, `sync-seed`-equivalent
re-scrapes the vendored copy every run.

## Recurring job shape (draft, mirrors `../DAILY_JOB.md`'s numbering)

```
0. Check what's stale
   - Official registry / Glama: cursor-paginate from last-seen cursor;
     nothing to mark "unsynced" the way repo clones are -- both are live
     paginated pulls, re-run picks up new/updated entries.
   - mcpservers.org: re-pull the sitemap; diff against last-seen URL set
     (its lastmod timestamps make this cheap).
   - npm/PyPI candidate lists: re-run search daily; new hits are additive.
   - Individual repos already scanned via scan_github_repo: no local clone
     to go stale (that's the point) -- re-scan on a schedule instead
     (weekly? readme/config genuinely doesn't change often).
   - Seed repo (awesome-mcp-servers): same two-layer staleness as
     officialskills.sh -- vendored copy can be stale even if sync ran daily
     (see ../DAILY_JOB.md step 3); refresh the vendored copy on its own
     schedule, then re-scrape it every run.

1. Pull sources (additive, rate-limited)
   a) Official MCP registry -- paginate fully, ~free, no rate limit concern
   b) Glama.ai API -- paginate fully; prefer its description/attributes/
      environmentVariablesJsonSchema over self-derived versions on match
   c) mcpservers.org sitemap -- pull URL list, corroborating signal +
      star count, lower priority (HTML scrape, no structured API)
   d) npm search "mcp" -- already built, 9.2 req/min pace
   e) PyPI equivalent -- proposed above, not yet built
   f) GitHub search "mcp-server" / "model context protocol server" --
      ranking signal + last-resort discovery for registry-less repos
   g) Seed repo (punkpeye/awesome-mcp-servers) -- vendor + scrape, same
      pattern as officialskills.sh; expect heavy overlap with (b), still
      worth pulling for its long tail and independent curation signal

2. Extract per candidate
   - Official registry / Glama hits: already structured, use as-is
     (Glama's description/attributes/env-schema take priority over
     self-derived versions where both exist -- see source notes above).
   - npm/PyPI hits with no Glama/official-registry match: scan_github_repo
     against their repository url (no clone) for server.json/package.json;
     backfill_readmes.py's 2-tier fallback (GitHub raw -> tarball) for
     anything without one.
   - GitHub-search-only hits (no registry presence anywhere): scan_github_repo
     directly; clone only if that comes back empty (see table above).

3. Classify + describe (see proposals below)
   - classify_mcp.py's rule-based categories, manual_classifications.py
     for the residual "unclear" bucket.
   - Description extraction (below) -- only needed where Glama has no match.
   - Configuration/auth column (below) -- only needed where Glama has no
     match; otherwise use its environmentVariablesJsonSchema directly.
   - Deployment mode column (below) -- only needed where Glama has no
     match; otherwise use its hosting:* attribute directly.

4. Export
   - export_csv.py-equivalent, regenerated in full each run (same
     "regenerate the whole CSV" pattern as ../export_csv.py, not an
     incremental diff -- dataset size doesn't yet justify incremental).
```

## New column: description extraction (readme/npm) -- start with 10 cases
`description` today is just whatever the registry (npm/PyPI/official
registry) has on file, verbatim -- and that's frequently unusable as-is:
- Missing entirely for some npm packages.
- Badge-and-banner soup instead of prose (`@univerjs-pro/mcp`'s
  `description` field in the current CSV is literally three shield.io
  badge markdown links, no text).
- A generic monorepo-wide description that doesn't describe *this*
  specific package (`@univerjs-pro/sheets-mcp` inherits Univer's
  "full-stack isomorphic office SDK" tagline, not "the sheets MCP tools").
- Describes what the package *talks to*, not what it *is* -- the exact
  trap `classify_mcp.py` hit with `@ai-sdk/mcp` ("lets you connect to MCP
  servers" reads like a server description out of context).

**Proposed approach: don't write a general extraction rule yet.** Start
with 10 real cases spanning the failure modes above (e.g. from the current
100-row dataset: `mongodb-mcp-server` as the clean baseline,
`@univerjs-pro/mcp` for badge-soup, `chrome-devtools-mcp` for a good
one-liner already, `@ai-sdk/mcp` for the client/server-confusion trap,
`atlassian-mcp-server` for remote-only, plus 5 more picked for variety),
extract a clean 1-2 sentence description for each by hand/readme-review,
and only then look for a shared pattern (first non-badge paragraph after
the title? first sentence of the "## Overview" section if present? etc.)
worth automating. Trying to guess the general rule before seeing 10 real
failure cases risks over-fitting to whichever example is top of mind --
same lesson as the classify_mcp.py priority-order bugs, which only surfaced
by testing against the actual data rather than reasoning about it
abstractly. Bring this to review once the 10 are done, before writing an
automated extractor.

## New column: configuration details (auth, required setup)
`server.json`'s `packages[].environmentVariables[]` already carries most of
this in structured form for registry-listed servers (name, description,
`isRequired`, `isSecret`, `format`) -- `scan_mcp.py` already captures the
raw list (`env_vars_json`). Proposed additions:
- `requires_auth`: derived boolean -- any environmentVariable with
  `isSecret: true`, or a name matching `/api[_-]?key|token|secret|client[_-]
  ?id|oauth/i`.
- `auth_method`: best-effort classification from the env var names/readme
  -- `"api_key"`, `"oauth"`, `"connection_string"` (mongodb-style), or
  `"none"` if no secret-flagged vars exist.
- `config_summary`: the non-secret required env vars, human-readable (e.g.
  "requires MDB_MCP_CONNECTION_STRING" for mongodb) -- gives a search-db
  user a quick read on setup burden without opening the repo.

For packages with no `server.json` (package.json-only extraction), this
column would need README pattern-matching for a `.env`/config table --
noticeably lower-confidence than the structured server.json path, so
should probably carry a `config_source: "server.json" | "readme-guess" |
"unknown"` alongside it so low-confidence rows are distinguishable.

## New column: deployment mode
`scan_mcp.py` now derives this per-repo (added while fixing the Atlassian
edge case -- see `MCP_PIPELINE.md`) but hasn't been backfilled onto the
npm dataset yet. Three values:
- **`local`** -- runs on the user's machine (stdio transport, or a
  `packages[]` entry with no remote transport). The common case --
  `mongodb-mcp-server`, most npm/PyPI results.
- **`remote`** -- a hosted endpoint the client connects to
  (`packages[].transport.type` is `streamable-http`/`sse`, or the server
  is entirely `remotes[]`-based like Atlassian's). Includes both "the repo
  ships an installable proxy to a remote backend" and "there's no
  installable package at all, only a URL."
- **`built-in`** (proposed, not yet distinguished from `remote`) -- the
  MCP capability is bundled inside a larger commercial product rather than
  offered as a standalone integration point (e.g. a product's in-app AI
  assistant that happens to speak MCP internally, discoverable only via a
  product announcement/blog post, not a repo or registry entry at all).
  This is the hardest of the three to detect systematically -- likely
  needs a manual-classification-table entry per case rather than a rule,
  at least initially.

## Edge case catalog (seed it with what's already found; keep growing)
| Pattern | Example | Signal | Handling |
|---|---|---|---|
| Closed-source, remote-hosted | `atlassian/atlassian-mcp-server` | `server.json` has `remotes[]`, no `packages[]`; no package.json | `deployment: "remote"`, `has_installable_package: false`, package_url stays null -- **don't** attribute an unrelated README install-snippet link to it (this was a real bug, now fixed + regression-tested) |
| Monorepo package with no own readme | `mongodb-mcp-server` | npm registry `readme` field empty despite npmjs.com showing one (site falls back to GitHub) | `backfill_readmes.py`'s GitHub-raw tier |
| No repo link anywhere | 2 of the 100 npm candidates | no `repository`, no `homepage` pointing at GitHub | `backfill_readmes.py`'s tarball-extraction tier (works regardless of repo linkage) |
| MCP-adjacent, not itself a server | `@hono/mcp` (middleware), `mcp-auth` (auth lib) | server-SDK dependency present, but tooling keyword + no bin | `classify_mcp.py`'s `tooling` category |
| Self-describes as client, mentions "servers" | `@ai-sdk/mcp` | description says "connect to MCP servers" | `classify_mcp.py` suppresses the server-keyword match when "client" appears in text |
| Depends on a framework vs. *is* the framework | `@storybook/mcp` (uses `tmcp`) vs. `tmcp` itself | package name vs. dependency name matching | `classify_mcp.py` distinguishes `is_third_party_framework` from `uses_framework_dep` |
| Third-party directory's repo link/stats untrustworthy | `glama.ai/mcp/servers/broisnischal/openapi-mcp` | Glama's own `repository.url` points to a repo whose `package.json` name is `"hono"`, not `openapi-mcp`; page also references a third, unrelated repo (`minamorl/openapi-mcp-bridge`) | Never trust a directory's `repository.url` (Glama or otherwise) without verifying it against the claimed package's own manifest, same check `scan_mcp.py` already does; pull downloads/stars from the authoritative source directly (`api.npmjs.org`, GitHub API), never from a directory site's displayed number |

## Open questions before implementing
1. Should the official registry pull fully replace npm/PyPI search
   eventually, or always run in parallel (since registry coverage is
   presumably incomplete for a while yet)? Proposing parallel, revisit
   once we have a sense of overlap %.
2. How much of the npm/PyPI candidate set does Glama already cover? Only
   spot-checked (query filter works, ~28/50/14/8 hosting-attribute split on
   an unfiltered sample) -- worth a real overlap measurement against the
   existing 100-row npm dataset before deciding how much self-derived
   extraction work Glama actually saves in practice, versus how much is
   still needed for the long tail Glama's review process hasn't reached.
3. mcpservers.org and Glama are both third-party community sites, not
   official MCP project infrastructure -- haven't located a formal terms-
   of-use/rate-limit statement for either's public data (Glama's `/api/`
   `robots.txt` disallow is about crawling their site, not necessarily
   about hitting the API directly with reasonable, paced request volume,
   but this is a reasonable-use assumption, not a confirmed ToS reading).
   Worth a deliberate, conservative pace (same discipline as the npm
   fetch) and revisiting if either site publishes explicit API terms.
4. Persistence -- still deliberately not decided (see `MCP_PIPELINE.md`).
   This proposal assumes CSV/JSON output continues until that's settled,
   same as the current prototype.
5. Where does PyPI's `pypi.org/simple/` full-index pull run -- is a ~30MB
   one-time-per-day download and local grep acceptable, or does that need
   its own caching/staleness story (analogous to `refresh_seeds.py`'s
   vendored-copy pattern in the main pipeline)?
