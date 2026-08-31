<!-- Hand-maintained. Keep in step with vettd packages/api/src/directory/search-beta.ts
     and vettd-cli src/directory.rs. Contract-sync process:
     vettd-e2e/KEEPING_API_DOCS_IN_SYNC.md -->

# End-to-end search flow: `vettd` CLI → vettd API → query service → Qdrant

How `vettd directory search` reaches this repo's Qdrant collections, and how
the **`SEARCH_BETA_TESTING`** flag changes the whole path — from a
Postgres-only `GET` (flag off, byte-identical to pre-beta) to a `POST` that
joins the `agent_skills` / `mcp_servers` catalog served by
`app/query_service.py` (flag on).

There are **two** `SEARCH_BETA_TESTING` flags and both must be on for the
beta path: the CLI's (`1` or `true`) and the vettd server's (exactly
`"true"`). `inventory search` is the session-authed twin of `directory
search` (the user's own skills); `assetType: "mcp"` is rejected there.

```mermaid
flowchart TD
    dev(["dev runs<br/>vettd directory search q"])
    cliFlag{"SEARCH_BETA_TESTING<br/>set on the CLI?<br/>(1 or true)"}
    dev --> cliFlag

    %% ---------------- FLAG OFF (default) ----------------
    cliFlag -->|"unset · DEFAULT"| get["GET endpoint /api/directory<br/>search / sort / page in query string"]
    get --> apiGet["vettd Next.js<br/>GET /api/directory<br/>getDirectorySkills"]
    apiGet --> pg[("vettd Postgres<br/>public directory feed")]
    pg --> getResp["DirectoryListResponse<br/>skills / total / page / totalPages<br/>plain cards · no catalog fields · no mock key<br/><b>byte-identical to pre-beta</b>"]
    getResp --> dev
    apiGet -. "query service never called" .-> qsSkill

    %% ---------------- FLAG ON ----------------
    cliFlag -->|"= 1 or true"| post["POST endpoint /api/directory<br/>JSON body: search, page, sort, assetType,<br/>languages, agentCompatibility, sources,<br/>rankFilters, mcpCategory, deployment,<br/>registryType, rankings"]
    post --> srvFlag{"vettd server<br/>SEARCH_BETA_TESTING<br/>equals true ?"}
    srvFlag -->|no| r404["404 Not Found"]
    r404 --> dev
    srvFlag -->|yes| asset{"assetType"}

    asset -->|"skill · DEFAULT"| skillPage["getDirectorySkills → one Postgres page<br/>then attachSearchFields"]
    asset -->|"mcp"| mcpProxy["searchMcpCatalog<br/>thin proxy · no Postgres spine"]
    skillPage --> qsSkill
    mcpProxy --> qsMcp

    subgraph qs ["ah-skills search-demo · app/query_service.py · POST /query"]
        direction TB
        qsSkill["asset_type: skill<br/>+ languages, agent_compatibility,<br/>sources, rank_filters, min_stars"]
        qsMcp["asset_type: mcp<br/>+ mcp_category, deployment,<br/>registry_type, sources, min_stars"]
        qsSkill --> qdrantSkill[("Qdrant: agent_skills<br/>hybrid dense + BM25")]
        qsMcp --> qdrantMcp[("Qdrant: mcp_servers")]
    end

    qdrantSkill --> joinSkill["join hits to Postgres skills<br/>by normalized repo URL"]
    joinSkill --> skillResp["DirectorySearchListResponse<br/>cards + docLanguage · agentCompatibility · rankings<br/>+ llm_scan · cli_security · vettd_scan (passthrough)<br/>+ mock flag"]
    skillResp --> dev

    qdrantMcp --> mcpResp["McpSearchListResponse<br/>mcpServers: McpHit array verbatim<br/>+ total / page / totalPages<br/>+ mock: false + indexReady"]
    mcpResp --> dev

    qsSkill -. "unreachable / non-200 / unconfigured" .-> failOpen["<b>fail-open</b><br/>cards still returned, catalog fields empty,<br/>filters become no-ops — never 5xx,<br/>never a false zero-results from an outage"]
    failOpen --> dev
    qsMcp -. "unreachable" .-> mcpEmpty["empty page · indexReady: false"]
    mcpEmpty --> dev

    skillPage -. "SEARCH_BETA_MOCK_DATA = true" .-> mock["deterministic fake enrichment<br/>mock: true · query service NOT called"]
    mock --> dev
```

## The two paths, side by side

| | flag **off** (default) | flag **on** (`SEARCH_BETA_TESTING`) |
|---|---|---|
| HTTP | `GET /api/directory?…` | `POST /api/directory` + JSON body |
| vettd handler | `getDirectorySkills()` | `getDirectorySkills()` **+ `attachSearchFields()`** (skill) / `searchMcpCatalog()` (mcp) |
| data sources | vettd Postgres only | Postgres page **+** this repo's `/query` **+** Qdrant `agent_skills` / `mcp_servers` |
| this repo touched? | **no** | **yes** — `query_service.py` → Qdrant |
| response | `DirectoryListResponse` — plain cards, no `mock` key | `DirectorySearchListResponse` (skill) / `McpSearchListResponse` (mcp), catalog-enriched, `mock` flag |
| server flag off | (n/a — GET is unaffected) | `POST` → **404** |
| catalog down | (n/a) | skill: **fail-open** (empty fields, no filtering); mcp: empty page, `indexReady: false` |

## Where each hop lives

| Hop | Code |
|---|---|
| CLI request shape + `SEARCH_BETA_TESTING` gate | `vettd-cli/src/directory.rs`, `src/network.rs` |
| vettd route | `vettd/apps/web/app/api/directory/route.ts`, `…/inventory/route.ts` |
| Postgres page + catalog join + fail-open + mcp proxy | `vettd/packages/api/src/directory/search-beta.ts` |
| `POST /query` (`asset_type`, filters, `SkillHit` / `McpHit`) | `app/query_service.py` (this repo) |
| hybrid search + payload → hit | `app/search.py` / `app/mcp_search.py` |
| Qdrant collections | `agent_skills` (skills pipeline) / `mcp_servers` (`mcp-search/`) |

Full HTTP contract: [`QUERY_SERVICE_API.md`](QUERY_SERVICE_API.md) (this repo,
generated) · `vettd/docs/VETTD_API.md` (vettd side, generated) ·
`vettd/docs/search-beta-api-spec.md` (prose).
