# `obra/superpowers` — unified scan-target tree (all in-scope harnesses)

`superpowers` ships one repo that is simultaneously a plugin for ~10 agent
harnesses. They share the `skills/` tree and the bundled scripts; each adds
its own manifest and (usually) its own **context-injection entrypoint** that
loads `using-superpowers/SKILL.md` into the model at session start.

This is the single tree for the scanner. Pinned at commit `b36e082` (v6.3.0).
Companion to [`SUPERPOWERS_SCAN_TARGETS.md`](./SUPERPOWERS_SCAN_TARGETS.md)
(Claude Code only) and [`SUPERPOWERS_INSTALLED_ASSETS.md`](./SUPERPOWERS_INSTALLED_ASSETS.md).

> **Scope: coding agents only.** `openclaw` and `hermes` are **excluded** — the
> same exclusion the skill / MCP pipelines apply. In this repo that drops
> `.hermes-plugin/` (`plugin.yaml` + `__init__.py`). It is listed under
> [Explicitly NOT scanned](#explicitly-not-scanned) as present-but-out-of-scope,
> not as a scan target. In-scope harnesses below: Claude Code, Cursor, Codex,
> Devin, Kimi, opencode, pi, Gemini CLI, and the `.agents/` marketplace.

**Tags:** `[manifest]` schema + lure + path-escape check · `[wiring]` hook /
context-import binding · `[code]` code-threat pass (runs on the user's machine) ·
`[skill]` reuse the skill scanner · `[det]` static check · `[inject]` this asset
loads text into the model automatically — also assess it as an injected payload.

---

## The tree

```
superpowers/
│
├── ═══ SHARED — scanned once, feeds every harness's verdict ═══
│
├── skills/                                    [skill]  ← all 34 files; see SUPERPOWERS_SCAN_TARGETS.md for the per-file list
│   ├── */SKILL.md .......................... [skill]  14 skills
│   ├── */*-prompt.md, */*-reviewer*.md ..... [skill]  subagent prompt templates
│   ├── */references/*.md, */*.md ........... [skill]  loadable reference docs
│   ├── brainstorming/scripts/
│   │   ├── server.cjs ..................... [code]   local HTTP/WS server + browser launch + shell-out  ← largest risk
│   │   ├── start-server.sh ................ [code]
│   │   ├── stop-server.sh ................. [code]
│   │   ├── helper.js ...................... [code]   browser-side WS client
│   │   └── frame-template.html ........... [det]
│   ├── subagent-driven-development/scripts/
│   │   ├── sdd-workspace .................. [code]   writes into user's repo tree, runs git
│   │   ├── task-brief .................... [code]
│   │   └── review-package ................ [code]
│   ├── systematic-debugging/find-polluter.sh  [code]  runs caller-supplied test commands
│   └── writing-skills/render-graphs.js ..... [code]   shells out to graphviz `dot`
│
├── assets/superpowers-small.svg ............. [det]    referenced by claude / codex / cursor manifests
│
├── package.json ............................ [det]    no scripts/deps → install-time npm-install is a no-op;
│                                                      `main` + `pi` keys are the opencode/pi entrypoints
│
├── ═══ CLAUDE CODE ═══
│
├── .claude-plugin/
│   ├── plugin.json ........................ [manifest]
│   └── marketplace.json .................. [manifest]  source:"./" (repo is its own marketplace)
├── hooks/
│   ├── hooks.json ........................ [wiring]    SessionStart, matcher startup|clear|compact
│   ├── run-hook.cmd ..................... [code]       cmd/bash polyglot wrapper  (SHARED with Cursor)
│   └── session-start ................... [code][inject]  emits using-superpowers/SKILL.md as additionalContext  (SHARED with Cursor)
│
├── ═══ CURSOR ═══
│
├── .cursor-plugin/plugin.json ............. [manifest]  hooks → ./hooks/hooks-cursor.json
├── hooks/hooks-cursor.json ................ [wiring]    sessionStart → run-hook.cmd session-start
│   └── (run-hook.cmd + session-start: shared, see Claude Code above)
│
├── ═══ CODEX ═══
│
├── .codex-plugin/plugin.json .............. [manifest]  skills:"./skills/", hooks:{} (no hook), interface{} block
│
├── ═══ DEVIN ═══
│
├── .devin-plugin/plugin.json .............. [manifest]  minimal — no skills/hooks keys
│
├── ═══ KIMI ═══
│
├── .kimi-plugin/plugin.json ............... [manifest][inject]  sessionStart.skill:"using-superpowers"
│                                                       + large "skillInstructions" prose blob injected as tool-mapping guidance
│
├── ─── .hermes-plugin/  →  OUT OF SCOPE (hermes excluded); see "Explicitly NOT scanned" ───
│
├── ═══ OPENCODE ═══
│
├── .opencode/
│   ├── plugins/superpowers.js ......... [code][inject]  JS; message-transform injects bootstrap, config hook
│   │                                                    auto-registers skills dir. Installed via git+https npm spec.
│   └── INSTALL.md ..................... [det]           install instructions (npm install --prefix …) — read for install-vector, not a payload
│
├── ═══ PI ═══
│
├── .pi/extensions/superpowers.ts ......... [code][inject]  TS; pi.on("context") injects bootstrap,
│                                                        resources_discover registers skillsDir
│
├── ═══ GEMINI ═══
│
├── gemini-extension.json ................. [manifest]   contextFileName: "GEMINI.md"
├── GEMINI.md ............................ [wiring][inject]  @./skills/using-superpowers/SKILL.md
│                                                        @./skills/using-superpowers/references/gemini-tools.md
│
└── ═══ "AGENTS" (agent-harness marketplace) ═══
    └── .agents/plugins/marketplace.json .. [manifest]   source url:"./", policy.authentication: ON_INSTALL
```

---

## Per-harness rollup

Every harness pulls in the shared `skills/` + scripts + `assets/`. This table
is only the **harness-specific** additions and the injection mechanism.

| Harness | Manifest(s) | Injection entrypoint | Mechanism | Runs code on install/session? |
|---|---|---|---|---|
| **Claude Code** | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` | `hooks/hooks.json` → `hooks/run-hook.cmd` → `hooks/session-start` | `SessionStart` command hook → `additionalContext` | ✅ bash script every session |
| **Cursor** | `.cursor-plugin/plugin.json` | `hooks/hooks-cursor.json` → `run-hook.cmd` → `session-start` | `sessionStart` hook → `additional_context` | ✅ bash script every session |
| **Codex** | `.codex-plugin/plugin.json` | — (`hooks: {}`) | skills dir only | ❌ |
| **Devin** | `.devin-plugin/plugin.json` | — | skills dir only | ❌ |
| **Kimi** | `.kimi-plugin/plugin.json` | manifest `sessionStart.skill` + `skillInstructions` | manifest-declared, no code | ❌ (but injects a prose blob) |
| ~~Hermes~~ | ~~`.hermes-plugin/`~~ | — | **excluded (not a coding agent)** | out of scope |
| **opencode** | `package.json` (`main`) | `.opencode/plugins/superpowers.js` | JS message-transform + config hook | ✅ JS plugin, git/npm install |
| **pi** | `package.json` (`pi` key) | `.pi/extensions/superpowers.ts` | TS `pi.on("context")` + `resources_discover` | ✅ TS extension, npm install |
| **Gemini** | `gemini-extension.json` | `GEMINI.md` | `@`-import of skill files into context file | ❌ |
| **"agents"** | `.agents/plugins/marketplace.json` | — | marketplace manifest | ❌ |

**Observation for the scanner:** every in-scope harness delivers the *same
payload* — `skills/using-superpowers/SKILL.md` wrapped in `<EXTREMELY_IMPORTANT>`
— through 5 different mechanisms (bash hook, JS plugin, TS extension, manifest
key, context-file import). A plugin scanner that only understands Claude Code's
`hooks/hooks.json` misses the injection on most of them. The injected payload
should be scanned **once** (`using-superpowers/SKILL.md`); each entrypoint should
be scanned for *what else* it does beyond that injection.

---

## Flat list by scan type

### `[code]` — 13 (runs on the user's machine)
```
skills/brainstorming/scripts/server.cjs
skills/brainstorming/scripts/start-server.sh
skills/brainstorming/scripts/stop-server.sh
skills/brainstorming/scripts/helper.js
skills/subagent-driven-development/scripts/sdd-workspace
skills/subagent-driven-development/scripts/task-brief
skills/subagent-driven-development/scripts/review-package
skills/systematic-debugging/find-polluter.sh
skills/writing-skills/render-graphs.js
hooks/run-hook.cmd                     (Claude Code + Cursor)
hooks/session-start                    (Claude Code + Cursor)   [inject]
.opencode/plugins/superpowers.js       (opencode)               [inject]
.pi/extensions/superpowers.ts          (pi)                     [inject]
```

### `[wiring]` — 4 (injection / hook binding)
```
hooks/hooks.json                       (Claude Code)
hooks/hooks-cursor.json                (Cursor)
GEMINI.md                              (Gemini)                 [inject]
.kimi-plugin/plugin.json :: skillInstructions + sessionStart    (Kimi — inside the manifest)  [inject]
```

### `[manifest]` — 9
```
.claude-plugin/plugin.json
.claude-plugin/marketplace.json
.cursor-plugin/plugin.json
.codex-plugin/plugin.json
.devin-plugin/plugin.json
.kimi-plugin/plugin.json
gemini-extension.json
.agents/plugins/marketplace.json
package.json
```

### `[skill]` — 34
Unchanged from [`SUPERPOWERS_SCAN_TARGETS.md`](./SUPERPOWERS_SCAN_TARGETS.md) —
the `skills/` tree is shared verbatim by every harness.

### `[det]` — 4
```
skills/brainstorming/scripts/frame-template.html
assets/superpowers-small.svg
.opencode/INSTALL.md
package.json                           (also listed under [manifest]; the check is "no install scripts")
```

**Total distinct scan targets: ~63** (34 skill + 13 code + 4 wiring + 9
manifest + 4 det, minus overlap on `package.json`), of 194 installed files.
Hermes excluded.

---

## Explicitly NOT scanned

Same as the Claude Code list, plus:

- **`.hermes-plugin/plugin.yaml`, `.hermes-plugin/__init__.py`** — hermes is
  excluded from scope (not a general-purpose coding agent), same as the skill /
  MCP pipelines. `__init__.py` is Python that runs on import and injects a
  `<EXTREMELY_IMPORTANT>` bootstrap via a `pre_llm_call` hook — **it would be a
  `[code][inject]` target if hermes were in scope.** Revisit if scope widens.
- `docs/README.opencode.md`, `docs/README.kimi.md`, `docs/porting-to-a-new-harness.md` — per-harness docs
- `tests/{opencode,pi,hermes,kimi,codex,codex-plugin-sync,devin,antigravity}/**` — per-harness test suites; **`tests/brainstorm-server/package.json` does declare a `ws` dependency + a `test` script, but it is not at any plugin root**, so no harness `npm install`s it
- `scripts/{package-codex-plugin.sh,sync-to-codex-plugin.sh}` — release tooling for the codex variant
- `.version-bump.json` — lists every manifest + its version field (useful as a *map of manifests to check*, not a scan target itself)

> **Install-vector note:** opencode and pi install the repo as a **git-backed
> npm package** (`superpowers@git+https://github.com/obra/superpowers.git`), so
> for those two the scanner's unit is the npm tarball / resolved git tree and
> `npm install` runs against the **root `package.json`** (no scripts/deps here —
> confirm per-version). Claude Code / Cursor / Codex / Devin / Kimi / Hermes /
> Gemini install by copying the plugin directory.
