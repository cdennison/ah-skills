# `obra/superpowers` — scan-target tree

Every asset that has to be scanned, and nothing else. Paths are relative to the
plugin root. Derived from [`SUPERPOWERS_INSTALLED_ASSETS.md`](./SUPERPOWERS_INSTALLED_ASSETS.md);
pinned at commit `b36e082` (v6.3.0).

**This file is the Claude Code surface only.** For the same repo scanned across
every in-scope coding-agent harness (Cursor, Codex, Devin, Kimi, opencode, pi,
Gemini CLI — `openclaw`/`hermes` excluded), see
[`SCAN_TARGETS_ALL_HARNESSES.md`](./SCAN_TARGETS_ALL_HARNESSES.md).

Tags: `[wiring]` hook-binding pass · `[code]` code-threat pass ·
`[skill]` reuse existing skill scanner · `[det]` deterministic check ·
`[inject]` also assess as an auto-injected model payload.

```
superpowers/
│
├── .claude-plugin/
│   ├── plugin.json ............................... [det]  manifest schema, path-override escape, description/keywords lure
│   └── marketplace.json ......................... [det]  source type, strict:false, cross-marketplace deps
│
├── package.json ................................. [det]  confirm no install scripts (postinstall/preinstall/prepare)
│
├── hooks/
│   ├── hooks.json ............................... [wiring]  SessionStart auto-trigger, command type, no http
│   ├── run-hook.cmd ............................. [code]    interpreter-discovery wrapper (cmd/bash polyglot)
│   └── session-start ........................... [code] [inject]  injects using-superpowers/SKILL.md + directive every session
│
├── skills/
│   ├── brainstorming/
│   │   ├── SKILL.md ............................. [skill]
│   │   ├── visual-companion.md .................. [skill]
│   │   ├── spec-document-reviewer-prompt.md ..... [skill]  subagent prompt template
│   │   └── scripts/
│   │       ├── server.cjs ....................... [code]   local HTTP/WS server + browser launch + shell-out  ← largest risk
│   │       ├── start-server.sh .................. [code]   backgrounds server, --host 0.0.0.0, --open
│   │       ├── stop-server.sh ................... [code]
│   │       ├── helper.js ........................ [code]   browser-side WS client
│   │       └── frame-template.html .............. [det]    static shell — check inline script / outbound loads
│   │
│   ├── dispatching-parallel-agents/SKILL.md ..... [skill]
│   ├── executing-plans/SKILL.md ................. [skill]
│   ├── finishing-a-development-branch/SKILL.md .. [skill]
│   ├── receiving-code-review/SKILL.md ........... [skill]
│   │
│   ├── requesting-code-review/
│   │   ├── SKILL.md ............................. [skill]
│   │   └── code-reviewer.md ..................... [skill]  subagent prompt template
│   │
│   ├── subagent-driven-development/
│   │   ├── SKILL.md ............................. [skill]
│   │   ├── implementer-prompt.md ................ [skill]  subagent prompt template
│   │   ├── re-review-prompt.md .................. [skill]  subagent prompt template
│   │   ├── task-reviewer-prompt.md .............. [skill]  subagent prompt template
│   │   └── scripts/
│   │       ├── sdd-workspace .................... [code]   writes into user's repo working tree, runs git
│   │       ├── task-brief ....................... [code]
│   │       └── review-package ................... [code]   git log / git diff between refs
│   │
│   ├── systematic-debugging/
│   │   ├── SKILL.md ............................. [skill]
│   │   ├── condition-based-waiting.md ........... [skill]
│   │   ├── defense-in-depth.md .................. [skill]
│   │   ├── root-cause-tracing.md ................ [skill]
│   │   └── find-polluter.sh ..................... [code]   runs arbitrary caller-supplied test commands
│   │
│   ├── test-driven-development/
│   │   ├── SKILL.md ............................. [skill]
│   │   └── writing-good-tests.md ................ [skill]
│   │
│   ├── using-git-worktrees/SKILL.md ............. [skill]
│   │
│   ├── using-superpowers/
│   │   ├── SKILL.md ............................. [skill] [inject]  the payload the SessionStart hook injects
│   │   └── references/
│   │       ├── antigravity-tools.md ............. [skill]
│   │       ├── codex-tools.md ................... [skill]
│   │       ├── gemini-tools.md .................. [skill]
│   │       ├── hermes-tools.md .................. [skill]
│   │       └── pi-tools.md ...................... [skill]
│   │
│   ├── verification-before-completion/SKILL.md .. [skill]
│   │
│   ├── writing-plans/
│   │   ├── SKILL.md ............................. [skill]
│   │   └── plan-document-reviewer-prompt.md ..... [skill]  subagent prompt template
│   │
│   └── writing-skills/
│       ├── SKILL.md ............................. [skill]
│       ├── anthropic-best-practices.md .......... [skill]
│       ├── persuasion-principles.md ............. [skill]  teaches more coercive skill language — scanner note
│       ├── testing-skills-with-subagents.md ..... [skill]
│       └── render-graphs.js .................... [code]   shells out to graphviz `dot`
│
└── assets/
    └── superpowers-small.svg .................... [det]   SVG can carry script
```

## Flat list by scan type

**[wiring] — 1**
- `hooks/hooks.json`

**[code] — 11**
- `hooks/run-hook.cmd`
- `hooks/session-start`
- `skills/brainstorming/scripts/server.cjs`
- `skills/brainstorming/scripts/start-server.sh`
- `skills/brainstorming/scripts/stop-server.sh`
- `skills/brainstorming/scripts/helper.js`
- `skills/subagent-driven-development/scripts/sdd-workspace`
- `skills/subagent-driven-development/scripts/task-brief`
- `skills/subagent-driven-development/scripts/review-package`
- `skills/systematic-debugging/find-polluter.sh`
- `skills/writing-skills/render-graphs.js`

**[skill] — 34** (14 `SKILL.md` + 20 bundled instruction/reference/prompt `.md`)
- `skills/brainstorming/SKILL.md`, `.../visual-companion.md`, `.../spec-document-reviewer-prompt.md`
- `skills/dispatching-parallel-agents/SKILL.md`
- `skills/executing-plans/SKILL.md`
- `skills/finishing-a-development-branch/SKILL.md`
- `skills/receiving-code-review/SKILL.md`
- `skills/requesting-code-review/SKILL.md`, `.../code-reviewer.md`
- `skills/subagent-driven-development/SKILL.md`, `.../implementer-prompt.md`, `.../re-review-prompt.md`, `.../task-reviewer-prompt.md`
- `skills/systematic-debugging/SKILL.md`, `.../condition-based-waiting.md`, `.../defense-in-depth.md`, `.../root-cause-tracing.md`
- `skills/test-driven-development/SKILL.md`, `.../writing-good-tests.md`
- `skills/using-git-worktrees/SKILL.md`
- `skills/using-superpowers/SKILL.md`, `.../references/{antigravity,codex,gemini,hermes,pi}-tools.md`
- `skills/verification-before-completion/SKILL.md`
- `skills/writing-plans/SKILL.md`, `.../plan-document-reviewer-prompt.md`
- `skills/writing-skills/SKILL.md`, `.../anthropic-best-practices.md`, `.../persuasion-principles.md`, `.../testing-skills-with-subagents.md`

**[det] — 5**
- `.claude-plugin/plugin.json`
- `.claude-plugin/marketplace.json`
- `package.json`
- `skills/brainstorming/scripts/frame-template.html`
- `assets/superpowers-small.svg`

**Total scan targets: 51** (of 194 installed files).

## Explicitly NOT scanned

- `docs/**` (~40 files) — design/plan markdown, not loaded
- `tests/**` (~70 files, incl. `tests/brainstorm-server/package.json`) — not run on install
- `scripts/**` — repo-maintenance scripts, not referenced by any skill/hook
- `skills/systematic-debugging/{test-pressure-1,test-pressure-2,test-pressure-3,test-academic}.md`, `CREATION-LOG.md` — skill eval fixtures
- `skills/systematic-debugging/condition-based-waiting-example.ts` — code sample in a doc
- `skills/writing-skills/{graphviz-conventions.dot,examples/CLAUDE_MD_TESTING.md}` — style file + example
- `assets/app-icon.png` — raster image
- `CLAUDE.md`, `AGENTS.md`, `README.md`, `RELEASE-NOTES.md`, `LICENSE`, `CODE_OF_CONDUCT.md`, `.github/**`, dotfiles — project metadata (a plugin's `CLAUDE.md` is not loaded)
- `.codex-plugin/`, `.cursor-plugin/`, `.devin-plugin/`, `.kimi-plugin/`, `.hermes-plugin/`, `.opencode/`, `.pi/`, `.agents/`, `gemini-extension.json`, `GEMINI.md` — other-harness files, inert under Claude Code (report presence only)
