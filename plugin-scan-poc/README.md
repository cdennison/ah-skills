# plugin-scan-poc

Proof-of-concept for a security scan of a **new asset type: the coding-agent
plugin.**

The existing pipeline scans two asset types — Agent Skills
(`search-demo/docs/ARCHITECTURE_LLM_SCAN.md`) and MCP servers
(`search-demo/mcp-search/`). A *plugin* is a container that can bundle skills,
slash-command files, subagents, **hooks**, MCP servers, LSP servers, background
monitors, and executables, wired together by a manifest. Scanning "the skills
inside a plugin" is not the same as scanning the plugin: the manifest, the
hook wiring, and the bundled scripts are attack surface that a per-`SKILL.md`
scan never looks at.

### Scope

- **Claude Code is the primary target**, because its plugin format is the most
  fully specified and its docs are the reference for how a plugin is assembled.
- **But a real plugin repo is usually multi-harness.** `obra/superpowers` (the
  first fixture) ships as a plugin for ~10 coding agents from one repo —
  Claude Code, Cursor, Codex, Devin, Kimi, opencode, pi, Gemini CLI, … — each
  with its own manifest and its own context-injection entrypoint, all sharing
  one `skills/` tree. The scanner has to see the whole repo, so this POC covers
  **every coding-agent plugin surface in the repo**, not just `.claude-plugin/`.
  See [`SCAN_TARGETS_ALL_HARNESSES.md`](./SCAN_TARGETS_ALL_HARNESSES.md).
- **`openclaw` and `hermes` are excluded** — same exclusion the skill and MCP
  pipelines already apply (see the `scan_top_skills.py` selection rules in
  `search-demo/docs/ARCHITECTURE_LLM_SCAN.md`). Not general-purpose coding
  agents; their plugin/agent manifests are out of scope for now. In the
  superpowers repo that means `.hermes-plugin/` is documented as present but
  **not a scan target**.

This POC exists to answer, before any scanner is built:

1. **How is a plugin actually assembled** — across each in-scope harness: what
   files exist, what references what, what runs when, and where each piece
   lands on the user's disk after install.
2. **Which parts need a non-deterministic (LLM) scan** vs. which are covered by
   a deterministic check (schema validation, a static rule, an existing
   skill/MCP scan) vs. which are out of scope.
3. What a plugin-scan step would look like alongside the two existing scans.

### Guiding principle for asset discovery

**Work out exactly which files land on a user's machine when the plugin is
installed — then reverse-engineer from that set what needs scanning.** A plugin
install is a file copy (`~/.claude/plugins/cache/<mkt>/<plugin>/<version>/`)
plus `npm install`; the asset set is "everything in that copy that runs, is fed
to the model, or wires those together". `discover_assets.py` builds that set
deterministically and [`E2E_VERIFICATION.md`](./E2E_VERIFICATION.md) confirms it
by actually installing the plugin in a sandbox and diffing.

The [Work tracking](#work-tracking) section is the live task list.

---

## Layout

```
plugin-scan-poc/
├── README.md                        # this file — the POC's working document
├── discover_assets.py               # deterministic asset finder: repo -> assets JSON (no scanning)
├── E2E_VERIFICATION.md              # install-the-plugin-and-diff confirmation of discover_assets.py
├── e2e/                             # the sandbox: Dockerfile, verify.sh, compare_install.py, run_e2e.sh
├── SUPERPOWERS_INSTALLED_ASSETS.md   # per-asset install + scan inventory for obra/superpowers (Claude Code)
├── SUPERPOWERS_SCAN_TARGETS.md       # Claude Code scan targets, as a tree
├── SCAN_TARGETS_ALL_HARNESSES.md     # unified scan-target tree across all in-scope coding-agent harnesses
├── assets/                          # generated discover_assets.py output (gitignored)
├── .gitignore
└── repos/
    ├── repos.json                   # registry of test-fixture plugin repos (tracked)
    ├── superpowers/                 # fixture 1 — cloned, gitignored (see repos.json)
    └── impeccable/                  # fixture 2 — cloned, gitignored
```

`repos/*/` are working copies pinned to the commit SHAs in `repos/repos.json`
(superpowers 6.3.0, impeccable 4.2.0). Gitignored; re-clone with the commands
there. Nested `.git` dirs are kept so the pins can be checked out.

## Recreate from scratch

Only source is committed. The test-fixture repo clones, the generated asset
catalogues, and the e2e install snapshots are all gitignored and regenerable.

```bash
cd plugin-scan-poc

# 1. Re-clone the test-fixture plugin repos and pin them (SHAs in repos/repos.json).
#    repos/repos.json is the source of truth — every entry has a `clone` command
#    and a `pinned_sha`.
git clone https://github.com/obra/superpowers.git   repos/superpowers
git -C repos/superpowers  checkout b36e0829c6d0140e93cfef2ca599b1b07d4a7797

git clone https://github.com/pbakaus/impeccable.git repos/impeccable
git -C repos/impeccable   checkout 8dac6ae7e020c43ab10ce9b41939f6fd42627b96

# 2. Regenerate the asset catalogues (needs only python3 stdlib).
mkdir -p assets
python3 discover_assets.py repos/superpowers -o assets/superpowers.assets.json
python3 discover_assets.py repos/impeccable  -o assets/impeccable.assets.json

# 3. Re-run the end-to-end verification (needs Docker + network for the image
#    build; installs each plugin in a throwaway sandbox and diffs the result).
#    --rmi also deletes the sandbox image afterwards.
e2e/run_e2e.sh repos/superpowers repos/impeccable --rmi
```

Expected: step 3 prints `RESULT: PASS` for every plugin
(`superpowers-dev__superpowers`, `impeccable__impeccable`) — 0 installed files
unaccounted for. Full expected numbers are in
[`E2E_VERIFICATION.md`](./E2E_VERIFICATION.md).

**Adding another fixture:** add an entry to `repos/repos.json` (name, url,
`clone`, `pinned_sha`), clone it, then
`python3 discover_assets.py repos/<name>` and
`e2e/run_e2e.sh repos/<name>`. Review the `blind_spots` and the
"INSTALLED, NOT ACCOUNTED FOR" list; tune `discover_assets.py`'s classification
tables until the diff is clean.

**What is gitignored** (`.gitignore` + `e2e/.gitignore`):
`repos/*/` (the clones), `assets/` and `*.assets.json` (generated),
`e2e/out/` (install snapshots), `__pycache__/`.

## `discover_assets.py`

```bash
python3 discover_assets.py repos/superpowers -o assets/superpowers.assets.json
```

Points at a repo, works out every **install surface** (a marketplace entry's
`source` dir, a `.claude-plugin/plugin.json` dir, a `.claude/`/`.cursor/`/…
standalone dir, a skill-source dir), enumerates every file inside them, and
classifies each as `[manifest]` / `[wiring]` / `[code]` / `[skill]` / `[web]` /
`[config]` with a `security_relevance` and a one-line description. It resolves
hook/manifest references so bundled scripts are not missed, and emits
`blind_spots` for hook targets it could not resolve. It **does not scan** — it
produces the target list a scanner then works through. `hermes` / `openclaw`
surfaces go to `excluded`.

Output: `install_surfaces`, `assets[]`, `orphan_files` (not copied by any
install — reported, not scanned), `related_out_of_plugin_scope` (a browser
extension / npm CLI / build pipeline in the same repo — separate asset types),
`blind_spots`, `excluded[]`.

Verified end-to-end (`e2e/run_e2e.sh`): for both fixtures, **every file a real
`claude plugin install` writes to disk is a catalogued asset or an explicit
exclusion** — see [`E2E_VERIFICATION.md`](./E2E_VERIFICATION.md).

### First fixture — `obra/superpowers`

Chosen because it is a real, widely-installed plugin that exercises every
component this POC cares about:

- a **SessionStart hook** (`hooks/hooks.json`) that shells out to
  `hooks/run-hook.cmd session-start`, which reads
  `skills/using-superpowers/SKILL.md` and emits it as
  `hookSpecificOutput.additionalContext` — i.e. it **injects a skill's full
  text into the model's context on every single session start**, unprompted.
- ~15 skills under `skills/`, several with deliberately forceful language
  (`<EXTREMELY-IMPORTANT>`, "This is not negotiable", "You have superpowers",
  `<HARD-GATE>`).
- bundled runnable code: `skills/brainstorming/scripts/server.cjs` (~26 KB
  Node HTTP server), `start-server.sh`, `stop-server.sh`, `helper.js`.
- parallel manifests for other harnesses (`.codex-plugin/`, `.cursor-plugin/`,
  `.devin-plugin/`, `.hermes-plugin/`, `.opencode/`, `.pi/`) — **out of scope**
  for a Claude Code scan, but useful for testing that the scanner reads only
  the Claude surface (`.claude-plugin/`, `hooks/`, `skills/`, `.mcp.json`).

---

## How a Claude Code plugin works

Everything below is from the official docs (see
[Reference docs](#reference-docs-read-for-this-poc) for the page-by-page
summary of what each one established).

### The plugin directory

A plugin is a directory. The **only** thing that must live in
`.claude-plugin/` is the manifest; every component directory sits at the
plugin **root**:

| Path (relative to plugin root) | What it is | Loaded how |
|---|---|---|
| `.claude-plugin/plugin.json` | manifest — identity + optional component-path overrides | always read first |
| `.claude-plugin/marketplace.json` | present when the repo is *also* its own marketplace | read by `plugin marketplace add` |
| `skills/<name>/SKILL.md` | model-invoked skills (preferred layout) | auto-discovered; namespaced `plugin:skill` |
| `commands/<name>.md` | flat slash-command files (legacy skill layout) | auto-discovered |
| `agents/<name>.md` | subagent definitions | auto-discovered |
| `hooks/hooks.json` | event → command/http/mcp_tool/prompt/agent bindings | auto-discovered |
| `.mcp.json` | MCP server definitions bundled with the plugin | auto-discovered |
| `.lsp.json` | LSP server definitions | auto-discovered |
| `monitors/monitors.json` | background processes started when the plugin is active | auto-discovered |
| `bin/` | executables added to the Bash tool `PATH` while the plugin is enabled | auto-discovered |
| `settings.json` | default settings applied when enabled (`agent`, `subagentStatusLine` only) | auto-discovered |
| `SKILL.md` (at root) | shorthand for a single-skill plugin | auto-discovered |

`.claude-plugin/plugin.json` fields: `name` (required, kebab-case, becomes the
skill namespace), `version`, `description`, `author {name,email,url}`,
`homepage`, `repository`, `license`, `keywords`, `metadata`, `defaultEnabled`.
Plus path overrides that change where components are loaded from:
`skills` (adds to default), `commands` / `agents` / `workflows` /
`outputStyles` (replace default), `hooks`, `mcpServers`, `lspServers`,
`experimental.themes`, `experimental.monitors`. Also `userConfig` (typed
config prompts, values reach hooks as `CLAUDE_PLUGIN_OPTION_*` env vars, can be
`sensitive`), `channels`, and `dependencies` (other plugins).

**Key point for scanning:** component paths are not fixed. A scanner must read
`plugin.json` first and follow `hooks` / `mcpServers` / `commands` / `agents` /
`skills` if they point somewhere other than the defaults, rather than
globbing `hooks/hooks.json` and stopping.

### How hooks are wired and where the files are

1. **The binding** lives in `hooks/hooks.json` (or wherever `plugin.json`
   `hooks` points). Its shape is identical to the `hooks` object in a user's
   `.claude/settings.json`:

   ```json
   {
     "hooks": {
       "SessionStart": [
         {
           "matcher": "startup|clear|compact",
           "hooks": [
             { "type": "command",
               "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" session-start" }
           ]
         }
       ]
     }
   }
   ```

2. **The event** — one of ~35 event types. The ones that matter most for a
   security scan, because they run automatically and/or can alter control
   flow: `SessionStart`, `Setup`, `UserPromptSubmit`, `UserPromptExpansion`,
   `PreToolUse` (can **deny** a tool call), `PostToolUse` /
   `PostToolUseFailure` / `PostToolBatch`, `PermissionRequest` /
   `PermissionDenied`, `Stop` / `SubagentStop` (can **prevent** the turn
   ending), `PreCompact` / `PostCompact`, `PreModelSwitch`, `SessionEnd`,
   `InstructionsLoaded`, `FileChanged`.

3. **The hook type** decides what "runs":
   - `command` — runs a shell command / script. Path is usually
     `"${CLAUDE_PLUGIN_ROOT}"/...` pointing at a bundled script.
   - `http` — POSTs the event JSON to a URL (subject to the user's
     `allowedHttpHookUrls` allowlist).
   - `mcp_tool` — calls a tool on a bundled/configured MCP server.
   - `prompt` — evaluates a prompt with an LLM.
   - `agent` — runs an agentic verifier with tools.

4. **The bundled script(s)** the `command` points at. In superpowers:
   `hooks/run-hook.cmd` (a cmd/bash polyglot wrapper) and `hooks/session-start`
   (the actual bash script). These are ordinary files in the repo with no
   naming convention beyond what the JSON references. **This is where a
   command hook's real behaviour lives, and it is code, not config.**

5. **Path variables** available in hook commands:
   `${CLAUDE_PLUGIN_ROOT}` (absolute path to the installed plugin dir),
   `${CLAUDE_PLUGIN_DATA}` (persistent per-plugin dir that survives updates),
   `${CLAUDE_PROJECT_DIR}` (the user's project root).

6. **What a hook can do back to Claude:** exit code `2` blocks a blockable
   event (stderr = reason); exit `0` + JSON on stdout gives structured control
   — `hookSpecificOutput.permissionDecision: "deny"`,
   `additionalContext` (injected into the model's context),
   `updatedInput` (rewrites the tool input before it runs),
   `continue: false`, `systemMessage`. So a hook can silently (a) feed the
   model text on every prompt/session, (b) rewrite tool inputs, (c) allow/deny
   tool calls, (d) exfiltrate the full tool input (file contents, commands)
   over `http`.

### Where a plugin lands after install

`/plugin marketplace add <src>` → clones the marketplace repo to
`~/.claude/plugins/marketplaces/<name>/` and registers it in
`~/.claude/plugins/known_marketplaces.json`.

`/plugin install <plugin>@<marketplace>` → resolves the entry's `source`
(relative path / `github` / git `url` / `git-subdir` / `npm` / `archive` /
`command`), **copies** the plugin into
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, runs
`npm install` for any bundled `package.json` **into that cached copy**, and
enables it in user/project/local settings.

Updates are gated by a resolved version: marketplace-entry `version` →
`plugin.json` `version` → git commit SHA → archive SHA-256 → command-output
hash → timestamp. A `command` source re-runs every session. Claude Code
checks for updates in the background once per session.

**Implications for scanning:**
- The scan target is the resolved plugin tree (post-`source` fetch), not
  necessarily the marketplace repo root — for `git-subdir` / `npm` / `archive`
  sources they differ.
- `npm install` runs bundled install scripts on the user's machine; a
  bundled `package.json` with a `postinstall` is executed at install time and
  is not covered by any skill/hook scan.
- `command` sources run an arbitrary local command to *produce* the plugin
  directory — the command string itself is attack surface and re-runs each
  session.
- Deferred / dynamic sources mean "what version did the user actually get" is
  not always answerable from the marketplace manifest alone.

---

## What needs a non-deterministic (LLM) scan

Working inventory. "LLM scan" = the same kind of non-deterministic
threat-model pass the skill scanner does (`POST /scan`); "deterministic" = a
schema check or static rule with no model in the loop; "reuse" = already
covered by the existing skill or MCP scan if we point it at the right files.

| Plugin component | Scan type | Why | Status |
|---|---|---|---|
| **`hooks/hooks.json` bindings** | **LLM** + deterministic | Deterministic: flag `http` hooks, `SessionStart`/`UserPromptSubmit`/`PreToolUse` presence, `${CLAUDE_PLUGIN_DATA}` writes. LLM: judge *intent* of the wired command/prompt/agent — is a `SessionStart` command injecting instructions, is a `PreToolUse` hook silently rewriting inputs, does an `http` hook exfiltrate tool input. | not started |
| **Bundled hook scripts** (`hooks/*`, whatever `command` points at) | **LLM** (code) | This is the actual behaviour of a `command` hook. Needs a code-threat pass: network calls, reading env/credentials, writing outside the plugin dir, obfuscation, `curl \| sh`. Not a config check. | not started |
| **`prompt`- and `agent`-type hooks** | **LLM** | The prompt text / agent instructions run with tools on an automatic trigger — prompt-injection and privilege-escalation surface. | not started |
| **`plugin.json` manifest** | deterministic + light LLM | Deterministic: schema, `source` sanity, `bin/` presence, `userConfig.sensitive` handling, path overrides pointing outside the plugin. LLM: does `description` / `keywords` misrepresent what the plugin does (lure). | not started |
| **`marketplace.json`** | deterministic | `source` type + target, `strict: false` (marketplace entry overrides plugin.json), cross-marketplace `dependencies`. Mostly static. | not started |
| **Skills inside the plugin** (`skills/`, `commands/`) | **LLM — reuse skill scanner** | Same content, same threats (indirect prompt injection, policy violations). Point the existing skill scan at the plugin's skill files. New wrinkle: forceful language that is *normal* for a skill becomes higher-risk when a hook auto-injects it (see superpowers). | not started |
| **Subagents** (`agents/*.md`) | **LLM** | System-prompt + tool grants for an agent the plugin can make the *main thread* via `settings.json` `agent`. Injection / over-broad tool access. | not started |
| **Bundled MCP servers** (`.mcp.json`, `mcpServers`) | **reuse MCP scanner** + LLM | Point the existing MCP server scan at them. LLM: the `command`/`args`/`env` (does it run a bundled binary, pass a `sensitive` user_config value on the command line). | not started |
| **`package.json` install scripts** | **LLM** (code) + deterministic | Deterministic: presence of `postinstall`/`preinstall`/`prepare`. LLM: what the script does — it runs on `npm install` into the cache at install time. | not started |
| **`bin/` executables** | **LLM** (code) | Added to the Bash `PATH` while enabled; can shadow system commands. | not started |
| **`monitors/monitors.json`** | **LLM** + deterministic | Long-running background command started automatically when the plugin is active. `command` string + `when` trigger. | not started |
| **`command`-type marketplace source** | **LLM** (code) | Arbitrary command run each session to generate the plugin dir. | not started |
| **`settings.json` / `plugin.json` `settings`** | deterministic | Only `agent` + `subagentStatusLine` honoured; flag when a plugin force-activates its own agent as the main thread. | not started |
| **`.lsp.json`** | deterministic (for now) | Runs a language-server binary from `PATH`; lower priority, mostly "is the binary bundled or expected pre-installed". | not started |
| Non-Claude manifests (`.codex-plugin/`, `.cursor-plugin/`, …) | **out of scope** | Not read by Claude Code. Only used to assert the scanner ignores them. | n/a |

**Starting point (per the ask): hooks.** The hook layer is unique to plugins
(skills and MCP servers are already scanned elsewhere), it runs automatically,
and it can alter control flow and context. First deliverable is a hooks scan:
parse `hooks.json` → resolve every referenced `command` / `prompt` / `agent` /
`http` target → deterministic flags for the dangerous shapes → LLM threat pass
over the wiring *and* the bundled scripts it points at.

---

## Relationship to the existing scans

| | Vettd deterministic (`publish_scans.py`) | LLM skill scan (`ARCHITECTURE_LLM_SCAN.md`) | MCP scan (`mcp-search/`) | **Plugin scan (this POC)** |
|---|---|---|---|---|
| Unit | one `SKILL.md` folder | one `SKILL.md` string | one MCP server repo | one plugin directory (manifest + all components) |
| New surface | — | — | — | **hooks, manifest wiring, bundled scripts, install scripts, `bin/`, monitors, marketplace source** |
| Reuses | — | — | — | skill scan for `skills/`, MCP scan for `.mcp.json` |

A plugin scan is mostly an **orchestrator**: fan out to the skill and MCP
scanners for those components, and add new passes for the plugin-only surface
(hooks first).

---

## Reference docs (read for this POC)

Every page below was read on 2026-09-05. `docs.claude.com/en/docs/claude-code/*`
now 301-redirects to `code.claude.com/docs/en/*`.

| Doc | URL | What it established for this POC |
|---|---|---|
| **Create plugins** | `code.claude.com/docs/en/plugins` | Plugin = a directory; **only `plugin.json` goes in `.claude-plugin/`**, every component dir (`skills/`, `commands/`, `agents/`, `hooks/`, `.mcp.json`, `.lsp.json`, `monitors/`, `bin/`, `settings.json`) sits at the plugin **root**. Skills are namespaced `plugin:skill`. Migrating standalone config → plugin moves `settings.json` hooks into `hooks/hooks.json` (same object shape). `settings.json` supports only `agent` + `subagentStatusLine`; `agent` can make a plugin agent the **main thread**. `--plugin-dir` / `--plugin-url` load a plugin for one session (dev/test). `bin/` is disallowed for claude.ai-org-distributed plugins. |
| **Plugins reference** | `code.claude.com/docs/en/plugins-reference` | Full `plugin.json` schema incl. path-override fields (`hooks`, `mcpServers`, `commands`, `agents`, `skills`, `experimental.monitors`…), `userConfig` (typed, `sensitive`, surfaces as `CLAUDE_PLUGIN_OPTION_*` env), `channels`, `dependencies`. **`hooks/hooks.json` format** with `matcher` + `hooks[]`. The ~35 **hook event types** (SessionStart, Setup, UserPromptSubmit, UserPromptExpansion, PreToolUse, PermissionRequest/Denied, PostToolUse/Failure/Batch, Stop, SubagentStart/Stop, PreCompact/PostCompact, PreModelSwitch, InstructionsLoaded, FileChanged, SessionEnd, …). **Hook types**: `command`, `http`, `mcp_tool`, `prompt`, `agent`. **Path vars**: `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}`, `${CLAUDE_PROJECT_DIR}`. User-config values can't be substituted in shell-form commands — must be read from env. MCP config inline in `plugin.json` or in `.mcp.json` (same `command`/`args`/`env` shape, path vars allowed). Agent frontmatter fields; `hooks`/`mcpServers`/`permissionMode` **not** supported for plugin agents. LSP `.lsp.json` schema. `monitors/monitors.json` = array of `{name, command, description, when}`, started automatically. Single-skill plugins can put `SKILL.md` at root. |
| **Plugin marketplaces** | `code.claude.com/docs/en/plugin-marketplaces` | **`marketplace.json` schema**: `name`, `owner{name,email,url}`, `plugins[]`, optional `metadata.pluginRoot`, `renames`, `allowCrossMarketplaceDependenciesOn`. **Plugin-entry `source` types**: relative path, `github` (`repo`/`ref`/`sha`), git `url`, `git-subdir` (sparse), `npm` (`package`/`version`/`registry`), `archive` (HTTPS zip, ≤256 MiB, optional `sha256`), `command` (runs a local command to generate the dir, re-runs per session, `mode: copy|link`). **On-disk layout**: `~/.claude/plugins/marketplaces/<name>/`, `~/.claude/plugins/known_marketplaces.json`, `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` (install copies here; `npm install` runs into this copy). **Version resolution order** (gates updates). `strict: false` lets a marketplace entry fully override `plugin.json` components. |
| **Hooks** | `code.claude.com/docs/en/hooks` | **Hook stdin JSON**: common `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, optional `agent_id`/`agent_type`; event-specific e.g. PreToolUse adds `tool_name`, `tool_input`, `tool_use_id`. **Exit codes**: 0 = ok (stdout parsed as JSON for structured control), 2 = block (stderr = reason), other = non-blocking error. **JSON output**: `hookSpecificOutput.permissionDecision` (`deny`), `permissionDecisionReason`, `additionalContext` (injected to model), `updatedInput` (rewrites tool input), `continue: false`, `systemMessage`, `terminalSequence`. **Blockable events**: PreToolUse, UserPromptSubmit, UserPromptExpansion, PreModelSwitch, Stop/SubagentStop, ConfigChange, PostToolBatch. **Security warning**: hooks run with the user's full permissions, receive full tool input (file contents, commands), can read env vars; workspace-trust gates project hooks; enterprise `allowManagedHooksOnly`; `allowedHttpHookUrls` / `httpHookAllowedEnvVars` allowlists restrict `http` hooks. "Treat hook scripts with the same rigor as CI/CD scripts." |

Not yet read / to pull in as the POC develops: `discover-plugins` (install
UX + the security section shown to users), `plugin-dependencies`,
`plugin-marketplaces#command-sources` detail, `sub-agents`, `skills`,
`settings` (the hook-related allowlist keys), `mcp`.

---

## Work tracking

Nothing done yet. Keep this section current — it is the POC's task list.

### Phase 0 — scoping (this README)

- [x] Clone first test fixture (`obra/superpowers`) into `repos/`
- [x] Document how a Claude Code plugin is assembled (manifest, component
      dirs, hook wiring, on-disk install layout)
- [x] Inventory every plugin component and classify scan type
      (LLM / deterministic / reuse / out-of-scope)
- [x] Summarise the reference docs
- [x] Full per-asset install + scan inventory for the first fixture
      ([`SUPERPOWERS_INSTALLED_ASSETS.md`](./SUPERPOWERS_INSTALLED_ASSETS.md))
- [x] Claude Code scan-target tree
      ([`SUPERPOWERS_SCAN_TARGETS.md`](./SUPERPOWERS_SCAN_TARGETS.md))
- [x] Unified scan-target tree across all in-scope coding-agent harnesses,
      openclaw/hermes excluded
      ([`SCAN_TARGETS_ALL_HARNESSES.md`](./SCAN_TARGETS_ALL_HARNESSES.md))
- [ ] Get the component inventory + scan-type classification reviewed
- [ ] Decide: standalone `plugin-scan-poc/` scripts, or a design doc under
      `search-demo/docs/ARCHITECTURE_PLUGIN_SCAN.md` per the
      design-doc-workflow preference
- [x] Second fixture (`pbakaus/impeccable`) — a monorepo whose marketplace
      ships only a `./plugin` subdir; stresses subdir sources, per-harness
      standalone copies, PostToolUse/Stop hooks, and separately-distributed
      components (browser extension / npm CLI / rust engine)
- [ ] Add more fixtures: one with a bundled `.mcp.json`, one with a
      `package.json` `postinstall`, and a deliberately-malicious synthetic plugin

### Phase 0.5 — deterministic asset discovery (`discover_assets.py`)  ✅

- [x] Install-surface model (marketplace `source`, `.claude-plugin/plugin.json`,
      `.claude/`/`.cursor/`/… standalone, skill-source dirs; hermes/openclaw excluded)
- [x] Per-file classification + `security_relevance` + description; hook/manifest
      reference resolution (incl. `${CLAUDE_PLUGIN_ROOT}`, wrapper-arg, `@`-imports)
- [x] `blind_spots` for unresolved hook targets; `orphan_files` +
      `related_out_of_plugin_scope` for non-plugin components in the same repo
- [x] E2E: Docker sandbox installs the real plugin, diff proves 0 installed
      files unaccounted for on both fixtures ([`E2E_VERIFICATION.md`](./E2E_VERIFICATION.md))
- [ ] More fixtures through the e2e harness; tune classification as misses appear
- [ ] Feed the `assets[]` list into the Phase 1+ scanners as their work queue

### Phase 1 — hooks scan (the starting point)

- [ ] Parser: `plugin.json` → resolve `hooks` path → parse `hooks.json`
      into (event, matcher, hook-type, target) rows
- [ ] Resolver: for `command` hooks, follow the referenced script path
      (through `${CLAUDE_PLUGIN_ROOT}` and wrappers like
      `run-hook.cmd`) to the real file(s)
- [ ] Deterministic flags: `http` hook target vs. allowlist,
      auto-trigger events present (SessionStart / UserPromptSubmit /
      PreToolUse), `updatedInput` / `permissionDecision` use,
      writes to `${CLAUDE_PLUGIN_DATA}` / outside plugin root
- [ ] LLM pass over the hook wiring (intent of the binding)
- [ ] LLM code-threat pass over the bundled hook scripts
- [ ] Run against `superpowers` — expected: flag the SessionStart hook that
      auto-injects `using-superpowers/SKILL.md` as `additionalContext`
- [ ] Verdict shape (mirror `llm_scan` / `cli_security`: one object,
      latest-only, severity + findings[])

### Phase 2 — the rest of the plugin surface

- [ ] Manifest scan (schema + `source` sanity + path-override checks +
      lure check on description/keywords)
- [ ] `marketplace.json` scan (`source` type, `strict:false`, deps)
- [ ] Wire the existing **skill scan** at the plugin's `skills/` + `commands/`
- [ ] Wire the existing **MCP scan** at the plugin's `.mcp.json` / `mcpServers`
- [ ] `package.json` install-script scan
- [ ] `bin/` executable scan
- [ ] `monitors/monitors.json` scan
- [ ] `command`-source marketplace scan

### Phase 3 — orchestration & integration

- [ ] Plugin-scan orchestrator: fan out to sub-scans, aggregate one verdict
- [ ] Decide where plugin scan results live (own collection? payload field?)
- [ ] Corpus: how many plugins, from where
      (`search-demo/repo-seeds/claude_plugins_marketplace.json` is a start)
- [ ] Eval fixtures in `skill-scan-eval/` style (safe + malicious plugins,
      `_expected.json`)
- [ ] Pipeline wiring (later — mirror `--with-scan` / `--with-cli-scan`)

---

## Notes / open questions

- **`--plugin-dir` / `--plugin-url`** bypass the marketplace entirely (load a
  local dir or a remote zip for one session). Any plugin corpus and scan is
  blind to these. Worth stating as an explicit limitation.
- **Version ambiguity**: `command` sources and unversioned git sources mean
  "the version the user ran" often isn't in the marketplace manifest. A scan
  verdict may need to be keyed on a content hash of the resolved tree, not a
  version string (same choice the skill scan made).
- **Forceful language is normal in skills** but changes risk profile when a
  hook auto-injects it. The scanner should consider hook wiring and skill
  content *together*, not in isolation — superpowers is the motivating example.
- **`strict: false`** marketplace entries can define components the plugin
  repo doesn't contain — the scan target is the *resolved* plugin, so this
  needs the marketplace entry merged in before scanning.
