# Competitive analysis: MCP server discovery, today

What it's actually like to search for an MCP server right now, using one
concrete query -- `zim` (as in the [ZIM archive
format](https://en.wikipedia.org/wiki/ZIM_(file_format)), the thing
`openzim-mcp` serves -- see `../test-data/openzim-mcp-cluster/` for that
server's own description-quality writeup). Every claim below was checked
live against each service's own public API (same endpoints this pipeline's
`pull_glama.py`/`pull_official_registry.py`/`search_npm.py` already use),
not just eyeballed from a screenshot, so the numbers are reproducible.

## Glama -- glama.ai/mcp/servers?query=zim

Queried the same public API `pull_glama.py` pulls from
(`glama.ai/api/mcp/v1/servers?query=zim`, paginated 50 at a time). 113 total
results. Top 10:

```
1  Zip Hive Scaffold           Dqrshan/ziphive-scaffold
2  bim-electrical-mcp          althafdamara/bim-electrical-mcp
3  bim-agent                   Daviidro/bim-agent
4  OpenZIM MCP Server          ObunagaLabs/openzim-mcp        <- a fork, 0 known stars
5  mail-archive                myeongmi-kim/mail-archive
6  ai-company-mcp               IM-D311/ai-company-mcp
7  nvidia-nim-mcp               david-eve-za/nvidia-nim-mcp
8  ios-sim-mcp                  rmathew1973/ios-sim-mcp
9  TASS MCP Server               SIM-DAD/tass
10 rn-sim-mcp                    atz-dsampath/rn-sim-mcp
```

None of `ziphive`, `bim-electrical-mcp`, `bim-agent`, `mail-archive`,
`ai-company-mcp`, `nvidia-nim-mcp`, `ios-sim-mcp`, `tass`, or `rn-sim-mcp`
contain the literal substring "zim" -- whatever's matching here is fuzzy/
typo-tolerant, not even plain substring search. Only #4 is actually
ZIM-related, and it's an unmodified fork (see `DESCRIPTION_COMPARISON.md`),
not the real server.

Where's the real one? Paged through all 113 results and found every
ZIM-related hit:

```
#3    OpenZIM MCP Server     ObunagaLabs/openzim-mcp     (fork)
#24   Zimbra MCP Server      PongsiriTK/zimbra-suite-mcp (unrelated -- Zimbra mail)
#26   zimage-mcp              sjemmeh/zimage-mcp           (unrelated -- image tool)
#28   openzim-mcp             flammafex/mcp-openzim        (disclosed fork, narrower scope)
...
#63,72  ZIM MCP Server        two different unrelated repos
#82   ZIM RAG MCP Server       gglessner/ZIM-MCP
#102  OpenZIM MCP Server      cameronrye/openzim-mcp   <-- the actual canonical, 105-star repo
```

**The best answer is #102 out of 113** -- second-to-last page. No sort
parameter exists on this endpoint at all (confirmed directly, and already
documented in `pull_glama.py`'s own module docstring); default order is
`createdAt` descending, so the newest-registered junk floats to the top and
the most established, most-corroborated server sinks to the bottom.
Re-sorting by any of Glama's displayed metrics on the website doesn't fix
this either -- the underlying API has nothing to sort *by* in the first
place; there's no relevance score, no star count, no download count
anywhere in the payload (confirmed -- see `mcp_stats.py`'s ranking-coverage
section, currently 0% across this whole pipeline for the same reason).

## Official MCP registry -- registry.modelcontextprotocol.io/?q=zim

Queried `v0/servers` with `search=zim`, `q=zim`, and `query=zim` directly.

- `q=` and `query=` are **silently ignored** -- results are just the
  unfiltered default listing (confirmed: `ac.inference.sh/mcp` appeared 4
  times in the top 10, one per historical version, because `version=latest`
  wasn't passed -- the exact duplicate-version trap
  `pull_official_registry.py`'s docstring already warns about).
- `search=` does *something*, but it's naive substring matching on the name,
  same failure mode as Glama: top 10 for `search=zim` were `ai.zimac/mnema`,
  `eu.ansvar/zimbabwe-law-mcp`, `hr.trazimstan/nekretnine` (twice --
  same version-duplication bug), `io.github.Evozim/*` (five entries, matching
  on the *username* "Evozim", not the server). `openzim-mcp` doesn't appear
  in the first 10 at all.
- No stars, downloads, or any ranking field anywhere in the schema (matches
  what `pull_official_registry.py` already captures -- see `mcp_registry.py`
  row keys). The one thing this registry *can* sort by is recency
  (`publishedAt`/`updatedAt`), and even that isn't exposed as a sort option
  in the UI.

Worse than Glama in one specific way: Glama's substring match at least finds
the real server somewhere in 113 results; the official registry's `search=`
doesn't surface it in the first 10 at all, and its unfiltered params
(`q`/`query`) don't filter anything.

## GitHub code/repo search -- `"zim" AND "mcp"`, sorted by stars

Queried `api.github.com/search/repositories?q="zim" "mcp"&sort=stars&order=desc`
directly. 27 total results, top 5:

```
105 stars  cameronrye/openzim-mcp              <- correct, #1
 41 stars  epheterson/Zimi
 19 stars  ThinkInAI-Hackathon/zim-mcp-server
 17 stars  zicojiao/zim-mcp-server
  3 stars  jeremie-lesage/zimbra-mcp
```

**This is the one search here that actually works** -- real ranking signal
(stars), and the canonical repo lands exactly where it should, #1. The
tradeoff: it's a literal-substring, phrase-quoted search, not semantic --
it would miss a server whose README talks about "offline Wikipedia
archives" or "Kiwix" without the literal string "zim" appearing in
indexed text GitHub happens to match on. It also has nothing to say about
which of a repo's *packages* to install, deployment mode, env vars, etc --
it's a repo finder, not a server-metadata source, which is exactly why this
pipeline treats GitHub search as a discovery channel (see
`PROPOSED_PIPELINE.md`) rather than a metadata source.

## npm search -- `"zim" AND "mcp"`

Queried `registry.npmjs.org/-/v1/search?text="zim" "mcp"` directly (the same
endpoint `search_npm.py` wraps). 83,579 "total" results. Top 10 contain
**zero** ZIM-related packages:

```
zego-zim-react-native   @modelcontextprotocol/sdk   zimjs
@openzim/libzim         @playwright/mcp              @storybook/addon-mcp
@modelcontextprotocol/ext-apps   chrome-devtools-mcp   @zimjs/game   @upstash/context7-mcp
```

This is npm's general full-text package search doing what it's built for --
finding popular, well-maintained packages that loosely match tokens -- not
finding a specific niche MCP server. `zim` matches `zimjs`/`zego-zim`, `mcp`
matches every well-known MCP-adjacent package regardless of relevance, and
the *popularity* of those unrelated packages (`@modelcontextprotocol/sdk`,
`chrome-devtools-mcp`) actively drowns out the one relevant, tiny package
that would only ever appear on `openzim-mcp`'s exact npm listing (which
doesn't even exist -- this server ships on PyPI, not npm, so no amount of
npm-search tuning would ever surface it).

## Context7 -- context7.com/?q=zim

Not really a competitor in the same category -- it's a documentation/code-
snippet retrieval tool for LLM coding assistants (index of library docs +
code snippets, one of Context7's own distribution channels happens to be an
MCP server), not an MCP-server discovery directory. Couldn't get the actual
`q=zim` result set (client-rendered SPA, no public search API found the way
Glama/npm/GitHub expose one), so no result table here.

Worth reading regardless:
[upstash.com/blog/context7-research](https://upstash.com/blog/context7-research)
describes how Context7 handles *retrieval quality* -- genuinely relevant
prior art for anything past keyword search. Short version: standard
vector-search RAG over docs plateaus on hard questions, so they added an
async feedback loop -- a background benchmark continuously scores live
query responses, and a low-scoring answer triggers a code-research agent
that investigates the actual repo and writes findings into a separate
"dynamic-context" index for future queries to hit directly. ~17% of served
snippets now come from that researched index, and their internal benchmark
score on hard questions went from 4.0 to 6.4 without hurting cost/latency
(self-reported numbers from their post, not independently verified here).
The interesting idea for this pipeline isn't the specific mechanism, it's
the framing: retrieval quality is measured and monitored continuously,
not asserted once and left alone -- something no discovery source examined
above does at all.

## Where this pipeline actually stands right now

Not exempt from any of the above -- checked directly, not assumed:

- **Ranking data: 0%.** Confirmed via `mcp_stats.py`'s ranking-coverage
  section before `fetch_mcp_rankings.py` (new, this pass) started backfilling
  it -- same gap as every source above except GitHub search.
- **Fork/duplicate detection: none.** The `openzim-mcp` cluster
  (`../test-data/openzim-mcp-cluster/DESCRIPTION_COMPARISON.md`) is a live
  instance of the exact same problem Glama has above -- four rows for one
  server, ranked (if this pipeline ranked anything today) with no signal
  telling them apart.
- **The live `mcp_servers` Qdrant collection is not currently usable for a
  real comparison.** Checked directly while building this doc: it holds
  62,329 points, but every sampled point (5,000/62,329 checked) carries the
  *skills* pipeline's payload shape (`content_hash`, `content`, skill-style
  `name`s like `pr-commit-workflow`) -- zero real MCP rows found. Both
  pipelines default to the same live Qdrant server
  (`http://localhost:6333`, no `MCP_QDRANT_URL`/`MCP_QDRANT_DB_PATH` set in
  `.env`) with distinct collection names (`agent_skills` vs `mcp_servers`),
  so this isn't a naming collision -- something (very likely the
  now-untracked `qdrant_db_agentcompat_test/` experiment visible in
  `git status`) wrote skills data into the `mcp_servers` collection on the
  shared server at some point. **Flagging, not fixing**: this needs a human
  decision (wipe and rebuild `mcp_servers`, most likely) before any
  semantic-search comparison against the sources above means anything --
  not something to silently drop as a side effect of an unrelated task.
- **What this pipeline could genuinely do better than every source above**,
  once ranking data and dedup exist: real semantic search (dense + sparse
  fusion, already wired in `index_qdrant.py`) over a deduped, ranked corpus
  that's richer than any single upstream source -- the corroboration story
  (`mcp_registry.py`'s multi-source `sources[]`) is the one thing none of
  Glama/official-registry/GitHub/npm individually have.
