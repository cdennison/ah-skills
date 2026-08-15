# MCP server discovery pipeline (prototype)

Extracts MCP (Model Context Protocol) server metadata, ahead of deciding
how it plugs into the main skills search db. Lives in its own directory
(`mcp-search/`, sibling to `../app/` and the rest of the skills pipeline)
deliberately kept separate -- see "Side by side with the skills pipeline"
in [`PROPOSED_PIPELINE.md`](PROPOSED_PIPELINE.md) for why. Shared code
between the two pipelines (currently just request-pacing) lives in
[`../shared/`](../shared/), not duplicated in each. Nothing here persists
to a db yet -- everything prints to console or writes plain JSON
files in this directory, by design, for easy review before committing to a
schema.

## Scripts

### `scan_mcp.py` -- single-repo extractor
Given one repo (local clone or `owner/repo` on GitHub), extracts MCP server
config and prints it as JSON.

Extraction priority:
1. **`server.json`** -- the official MCP registry manifest. Richest source:
   name, description, repository, `packages[].registryType/identifier/
   version/transport`, `environmentVariables` (name/description/required/
   secret/format). This is effectively ready-made install/launch config.
2. **`package.json`** fallback -- name, description, repository, bin/main
   (used to guess `registryType: npm`).

Also derives `package_url` (e.g. `npmjs.com/package/...`) from
`registryType` + `identifier`, falling back to a direct link scraped from
README.md when no registryType is available.

Two fetch modes, sharing all extraction logic via a `Fetcher` abstraction:
- **Local**: reads files from a cloned repo on disk (`scan_repo(path)`).
- **Remote, no clone**: fetches `server.json` / `package.json` / `README.md`
  directly from `raw.githubusercontent.com/<owner>/<repo>/HEAD/<path>`
  (`scan_github_repo(owner, repo)`, or `--github owner/repo` on the CLI).
  `HEAD` is a GitHub-served alias for the default branch, so no need to
  know `main` vs `master` up front. 404s resolve to "file not found",
  cleanly falling through the priority chain.

Verified against `mongodb-js/mongodb-mcp-server` (cloned into this dir):
both modes produce an identical extracted entry.

### `search_npm.py` -- npm registry search
Wraps the documented npm search endpoint
(`https://api-docs.npmjs.com/#tag/Search` → `GET
registry.npmjs.org/-/v1/search?text=...`). One request returns up to 250
hits with name/description/repo/npm links/downloads/score -- confirmed this
is the correct, current doc (not a stale/wrong link).

### `fetch_npm_mcp_candidates.py` -- top-N candidate fetch, rate limited
1. One `search_npm("mcp", size=100)` call for the hit list.
2. For each hit, `GET registry.npmjs.org/<package>` for full detail:
   description, license, homepage, repository, author, maintainers,
   keywords, engines, dependencies, readme, publish dates, and a
   `has_mcp_sdk_dependency` flag (dependency on `@modelcontextprotocol/sdk`).
3. Paced at 6.5s between package-detail requests (~9.2 req/min), safely
   under the "less than 1 req/s and less than 10/min" constraint given for
   this pipeline.

Output: `npm_mcp_candidates.json` (100 entries). Run once already;
re-running overwrites it.

### `backfill_readmes.py` -- fill the readme gaps
npm's registry doc doesn't always carry a readme (monorepo packages
published without one at that path -- confirmed for `mongodb-mcp-server`
itself, which is why npmjs.com's *website* falls back to rendering the
GitHub repo's README client-side when the registry API field is empty).
Two-tier backfill for whatever `fetch_npm_mcp_candidates.py` left empty:
1. **GitHub raw** -- `README.md` from `repository.url` (or `homepage` when
   that's the only GitHub link present), same source npmjs.com's website
   falls back to.
2. **npm tarball** -- for packages with no usable GitHub link at all
   (2 cases: one with no repo link anywhere, one hosted on a private
   GitLab instance), download the `.tgz` from the registry's `dist.tarball`
   and extract `package/README.md` directly. Works for any published
   package regardless of repo linkage.

Result: **100/100 entries now have a readme** (77 from npm directly, 21
from GitHub raw, 2 from tarball extraction) -- tracked per-entry in
`readme_source`.

### `classify_mcp.py` -- is this actually an MCP *server*?
The npm "mcp" search surfaces a lot of MCP-adjacent noise alongside real
servers: SDKs, clients, middleware, frameworks, dev tools. The original
`has_mcp_sdk_dependency` flag only checked `dependencies` for the literal
string `@modelcontextprotocol/sdk`, which undercounts for two real patterns
found in the data:
- **The SDK went v2 and split into subpackages** (`@modelcontextprotocol/
  server`, `/client`, `/core`, `/node`, `/express`, `/hono`, `/ext-apps`) --
  checking only the literal `/sdk` string misses all of these.
- **NestJS/DI-style packages declare it as a `peerDependency`**, not a
  `dependency` (e.g. `@rekog/mcp-nest` only lists it as a peer).

`classify_mcp.py` re-fetches full detail (adding `peerDependencies`, `bin`,
`keywords`) for the 32/100 entries that lacked the original signal, and
assigns one of five categories using deterministic signals only (no
per-package judgment calls):

| category    | meaning                                                          |
|-------------|-------------------------------------------------------------------|
| `server`    | implements an MCP server                                          |
| `client`    | implements an MCP client (talks to servers, isn't one)             |
| `framework` | a library *for building* MCP servers (e.g. `mcp-framework`, `tmcp`)|
| `tooling`   | MCP-adjacent utility/middleware/instrumentation, not a server itself |
| `unclear`   | insufficient deterministic signal; flagged for human review        |

Signals, in priority order:
1. `is_third_party_framework` -- package name itself matches a known
   framework (`mcp-framework`, `fastmcp`, `tmcp`/`@tmcp/*`,
   `@rekog/mcp-nest`) → `framework`.
2. `server_keyword_hit` -- name/description matches `mcp[\s-]server` /
   `server[\s-]mcp`, **and** the text doesn't also self-identify as a
   client (see below) → `server`.
3. `has_client_sdk_dep` (depends on `@modelcontextprotocol/client` without
   any server-side package) → `client`.
4. `client_word_hit` -- no server-side SDK dependency, but the word
   "client" appears in the description → `client`.
5. `has_server_sdk_dep` (official server-side SDK package, or a dependency
   on a known framework, in `dependencies` **or** `peerDependencies`) with
   a tooling keyword hit and no `bin` (nothing to launch standalone) →
   `tooling`; otherwise → `server`.
6. `tooling_keyword_hit` alone (adapter/middleware/instrumentation/
   inspector/proxy/tunnel/utils/plugin/toolkit/cli/generator/framework/sdk/
   client) → `tooling`.
7. Nothing matched → `unclear`.

**Bugs found and fixed while building this** (worth remembering if the
classifier gets extended):
- A raw `has_server_sdk_dep` match was overriding clear tooling signals --
  `@hono/mcp` (Hono middleware), `@expo/mcp-tunnel` (a client), and
  `mcp-proxy` all pull in the server-side SDK as a transport binding
  without being servers themselves. Fixed by demoting to `tooling` when a
  tooling keyword hits *and* there's no `bin` to actually run.
- The original keyword regex was space-only (`mcp\s+server`), missing the
  hyphenated form the ecosystem actually uses in package names
  (`fiori-mcp-server`, `mcp-server-kubernetes`).
- Depending on a framework package (e.g. `@storybook/mcp` depends on
  `tmcp`) was being conflated with *being* that framework. Fixed by
  splitting `is_third_party_framework` (name match -> the framework itself)
  from `uses_framework_dep` (dependency match -> folded into the server
  signal instead, since using a framework to build a server still makes
  you a server).
- `@ai-sdk/mcp` describes itself as an "MCP client" but its description
  also says it lets you "connect to MCP servers" -- a naive substring match
  on "mcp servers" mis-tagged it as a server. Fixed by suppressing the
  server-keyword match whenever the word "client" appears anywhere in the
  text.

Final run on the 100 candidates: **86 server, 6 tooling, 4 unclear, 2
client, 2 framework**. Supports `--no-fetch` to re-run the classifier logic
against already-cached `peer_dependencies`/`bin`/`keywords` without hitting
npm again (useful when iterating on the rules, as happened here).

Still genuinely unclear (real ambiguity, not a rule gap -- flagged for
manual review rather than guessed): `@playwright/mcp`, `@univerjs-pro/mcp`,
`@univerjs-pro/sheets-mcp`, `mcp-auth`.

### `manual_classifications.py` -- the human-in-the-loop override table
`classify_mcp.py`'s deterministic rules are intentionally conservative --
anything they can't confidently resolve lands in `unclear` rather than
guessing. `manual_classifications.py` is where those get resolved by
actually reading the package's readme, kept as a separate module (not
inline in the classifier or the CSV export) so:
- it's reusable by both `classify_mcp.py` (applies the override to
  `mcp_category` itself, stamping `mcp_category_source: "manual"` so
  provenance is never ambiguous) and `export_csv.py` (the `claude_opinion`
  column is just this table's `opinion` field, passed through)
- extending it later is a one-place edit, not a hunt across scripts

Current entries (all 4 "unclear" packages from the last full run, now
resolved):

| package | category | basis |
|---|---|---|
| `@playwright/mcp` | server | readme says outright "A Model Context Protocol (MCP) server..."; classifier missed it only because the *description* field alone didn't contain the literal phrase |
| `@univerjs-pro/mcp` | server (lower confidence) | no SDK dependency or bin, but is the base MCP-integration package `@univerjs-pro/sheets-mcp` builds on inside Univer's monorepo -- embedded platform capability, not standalone |
| `@univerjs-pro/sheets-mcp` | server (lower confidence) | same basis, sheets-specific layer on top of the above |
| `mcp-auth` | tooling | readme opens "MCP Auth Node.js SDK" -- an OAuth 2.1/OIDC library *for* MCP servers, not a server itself |

Applying these (`classify_mcp.py --no-fetch`, since the underlying
dependency/keyword data was already cached from the earlier full run) took
the dataset from 96 rule-classified + 4 unclear to **89 server / 7 tooling
/ 2 client / 2 framework, 0 unclear**.

### `export_csv.py` -- flatten to CSV for spreadsheet review
Repeatable: run after `fetch_npm_mcp_candidates.py` → `backfill_readmes.py`
→ `classify_mcp.py` to regenerate `npm_mcp_candidates.csv` from whatever's
currently in `npm_mcp_candidates.json`. 100 rows, one per candidate, all
fields gathered so far -- nested values (dependencies, peerDependencies,
bin, keywords, mcp_category_signals, maintainers) are JSON-serialized into
single cells so the file stays one-row-per-package instead of exploding
into a join table.

## A real edge case: closed-source, remotely-hosted servers
`atlassian/atlassian-mcp-server` (flagged for investigation) looks, at a
glance, exactly like every other repo `scan_mcp.py` handles -- it has a
`server.json`, a polished README, badges, one-click install buttons for
Cursor/VS Code/ChatGPT/Claude. But its `server.json` has **no `packages[]`
at all** -- only a `remotes[]` array:

```json
"remotes": [
  {"type": "streamable-http", "url": "https://mcp.atlassian.com/v1/mcp"},
  {"type": "streamable-http", "url": "https://mcp.atlassian.com/v1/mcp/authv2"}
]
```

There is no npm package, no PyPI package, no `package.json` (404) --
`https://mcp.atlassian.com/v1/mcp` *is* the server, closed-source, hosted
and operated by Atlassian. This repo is purely client-side config: install
buttons and docs pointing MCP clients at that URL. Confirmed via npm search
too -- every `atlassian-mcp` / `atlassian-mcp-server` hit on npm is a
**third-party reimplementation or proxy**, none of them the official one
(which, by definition, can't be on a package registry at all).

**This exposed a real bug**, not just a conceptual gap: `scan_mcp.py`'s
`extract_from_server_json` assumed `packages[0]` always exists
(`data.get("packages") or [{}]`), so a `remotes`-only server.json silently
produced an empty `pkg = {}`. With no derivable `package_url`, the
README-link fallback then fired -- and picked up the repo's own install
snippet (`npx -y mcp-remote ...`, a generic third-party stdio-to-remote
proxy tool, unrelated to Atlassian) and wrongly reported *that* as the
server's npm package. Fixed by:
1. Treating `remotes[]`-only as a distinct, real state
   (`has_installable_package: false`, `package_url: null`,
   `deployment: "remote"`, `remote_urls: [...]`) rather than an extraction
   failure to paper over.
2. Suppressing the README-link fallback entirely when `server.json`
   explicitly says there's no installable package -- absence of a package
   is information, not a gap to guess-fill.

Regression-tested (`test_remotes_only_server_json_has_no_installable_package`
in `test_scan_mcp.py`) using a fake fetcher with exactly this
remotes-only + misleading-README-snippet shape, so this can't silently
regress.

**Why this matters for the broader pipeline** (see
`PROPOSED_PIPELINE.md`): closed-source remote-hosted servers are a whole
category the npm/PyPI-search approach structurally can't find (there's no
package to search for), and can't be scanned by cloning either (there's no
server code in the repo -- it's just docs). The only way to discover them
is GitHub search on the term "mcp-server" / "model context protocol
server" turning up a repo, `scan_github_repo` reading its `server.json`,
and recognizing the `remotes`-only shape for what it is.

## Spike: seed repo end-to-end, first 3 entries
Per `PROPOSED_PIPELINE.md` source 6 (`punkpeye/awesome-mcp-servers`), ran a
quick spike (`spike_seed_repo.py`) to prove the seed-repo -> extraction
path end to end before building real vendoring/parsing machinery: pull the
seed README, regex out the first 3 `[owner/repo](github url)` entries
under the first category section (Aggregators), run `scan_github_repo`
(no clone) against each.

```bash
python spike_seed_repo.py            # first 3, default
python spike_seed_repo.py --count 5  # adjustable
```

Results, 3/3:
1. **`Correctover/mcp-server`** -- 404s on everything, including the repo
   itself (`api.github.com/repos/Correctover/mcp-server` -> 404). The repo
   is just gone -- normal staleness in a crowdsourced awesome-list, not a
   bug. **But it surfaced a real one**: `scan_mcp.py`'s CLI raises an
   uncaught `ValueError` on this, which would kill an entire batch run over
   one dead entry. `spike_seed_repo.py` wraps the call in try/except and
   continues, logging a skip instead -- the real pipeline needs the same
   per-entry isolation, not `scan_mcp.py`'s current single-repo-at-a-time
   let-it-raise behavior.
2. **`daedalusdevelopmentgroup/ddg-agent-payable-services`** -- extracted
   cleanly, and turned up a second real bug: its `server.json` has **both**
   a `packages[]` entry (pypi, stdio) *and* a `remotes[]` entry
   (`streamable-http` at `mcp.daedalusdevelopmentgroup.com`). This is
   exactly Glama's `hosting:hybrid` category (see source 1 in
   `PROPOSED_PIPELINE.md`) -- but `scan_mcp.py`'s `deployment` field was
   computed only from the packages side, so it silently reported `"local"`
   and dropped the fact a remote endpoint also exists. **Fixed**:
   `extract_from_server_json` now reports `"hybrid"` when both `packages[]`
   and `remotes[]` are present, matching Glama's own taxonomy exactly
   rather than inventing a different one. Regression-tested
   (`test_hybrid_deployment_when_packages_and_remotes_both_present`).
3. **`forgemeshlabs/coinopai-mcp`** -- extracted cleanly, npm package,
   local/stdio deployment, one required secret env var (wallet private
   key) -- an unremarkable, correctly-handled case, useful as the control
   against the two entries above that weren't.

**Takeaway**: a 3-entry spike already found one crash bug and one real
data-modeling gap, both now fixed -- the "start small before building the
real vendoring machinery" approach paid for itself immediately. Worth
running `spike_seed_repo.py --count 20` or more before committing to a
full seed-repo parser, on the expectation more edge cases are still in
there.

## Known gaps / not done yet
- No persistence layer (deliberately -- console/JSON output only, per
  current instruction, until a schema is agreed).
- `classify_mcp.py` only re-examines the 32 packages that lacked the
  original signal; the 68 that had `has_mcp_sdk_dependency: true` from the
  first pass are trusted as `server` without re-running the fuller signal
  set against them. Probably fine (that flag was already a real
  `@modelcontextprotocol/sdk` dependency), but worth a spot check later.
- Classification is deterministic-heuristic, not exhaustive -- e.g.
  `@modelcontextprotocol/ext-apps` (an SDK that *enables* servers to show
  UI, not a server itself) lands in `server` because its description
  mentions "MCP servers" without also containing "client". Residual noise
  like this is expected; the `unclear` bucket is the safety valve for cases
  the rules can't confidently resolve, not a guarantee of zero false
  positives elsewhere.
- Repo-level scanning (`scan_mcp.py`) and npm-level scanning
  (`fetch_npm_mcp_candidates.py` + `classify_mcp.py`) are not yet wired
  together -- e.g. nothing currently runs `scan_github_repo` against each
  npm candidate's `repository` url to pull `server.json`/env-var/transport
  detail for the ones classified `server`.
- `deployment`/`remote_urls`/`has_installable_package` only exist on
  `scan_mcp.py`'s per-repo output so far (added while fixing the Atlassian
  edge case) -- not yet backfilled onto the 100-row npm dataset. See
  `PROPOSED_PIPELINE.md`'s "deployment mode" column for where this is headed.
- Description extraction is still just "whatever npm/readme happens to have
  in the `description` field" -- no attempt yet to pull a clean one-line
  summary out of a full README when the registry description is missing,
  generic, or badge-soup (see `@univerjs-pro/mcp`'s description in the CSV
  for an example of the latter). `PROPOSED_PIPELINE.md` proposes starting
  this from 10 hand-reviewed cases rather than guessing at a general rule.

## Files in this directory
- `scan_mcp.py`, `test_scan_mcp.py` -- single-repo extractor + tests
  (local clone or `--github owner/repo`, no clone needed)
- `search_npm.py` -- npm search wrapper
- `fetch_npm_mcp_candidates.py` -- top-100 npm fetch (rate limited)
- `backfill_readmes.py` -- readme backfill (GitHub raw + tarball fallback)
- `classify_mcp.py` -- server/client/framework/tooling classifier, applies
  `manual_classifications.py` overrides to anything still `unclear`
- `manual_classifications.py` -- hand-reviewed category overrides, keyed by
  package name, with the reasoning kept alongside each one
- `export_csv.py` -- flattens the JSON dataset to `npm_mcp_candidates.csv`
  for spreadsheet review
- `npm_mcp_candidates.json` / `.csv` -- the actual 100-entry dataset
  (generated; regenerate with `fetch_npm_mcp_candidates.py` →
  `backfill_readmes.py` → `classify_mcp.py` → `export_csv.py`, in that order)
- `spike_seed_repo.py` -- quick end-to-end check of the seed-repo source
  (`punkpeye/awesome-mcp-servers`): pulls the first N entries, runs
  `scan_github_repo` against each, prints results; found and fixed two real
  bugs on its first 3-entry run (see "Spike" section above)
- `PROPOSED_PIPELINE.md` -- proposal for turning this prototype into a
  recurring, multi-registry pipeline (npm + PyPI + GitHub search + repo
  scanning), modeled on the main project's `../DAILY_JOB.md`
- `mongodb-mcp-server/` -- the reference clone used to validate `scan_mcp.py`
- `mcp_servers.db` -- leftover sqlite file from an earlier iteration that
  used to persist entries; unused by current code, safe to delete

## `../shared/` -- code shared with the skills pipeline
This directory sits alongside `mcp-search/`, not inside it, specifically so
it's usable by both pipelines without either importing from the other's
directory. Currently just:
- `rate_limit.py` -- `sleep_if_more(index, total, interval)`, extracted
  because the identical "sleep between requests, skip it after the last
  one" check existed three times, unchanged, across
  `fetch_npm_mcp_candidates.py`, `backfill_readmes.py`, and
  `classify_mcp.py` before this existed. The skills pipeline's own
  `clone_repos.py` paces GitHub API calls with similar hand-rolled logic
  today -- a candidate to migrate onto this too, not done as part of this
  change since it's out of scope for the MCP-side work.

Scripts that need it add the parent directory to `sys.path` at import time
(`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` before
`from shared.rate_limit import sleep_if_more`) since `mcp-search/` scripts
are run standalone (`python3 script.py`), not as an installed package --
no `pyproject.toml`/`setup.py` makes `shared` importable by name otherwise.
**Nothing has been moved out of the main skills pipeline into `shared/`
yet** -- it currently only holds code written for `mcp-search/`. Promote
something skills-pipeline-side into it only when a second, genuine
consumer exists; don't move code there speculatively.
