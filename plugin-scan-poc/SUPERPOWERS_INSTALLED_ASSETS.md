# `obra/superpowers` — what gets installed on a user's machine, and what needs a security scan

Companion to [`README.md`](./README.md). This is the per-asset inventory for
one plugin **as installed by Claude Code**: everything `/plugin install
superpowers` writes to disk, whether it runs, when, and whether it needs a
security scan. The same repo is also a plugin for ~8 other coding agents — for
the cross-harness scan-target tree see
[`SCAN_TARGETS_ALL_HARNESSES.md`](./SCAN_TARGETS_ALL_HARNESSES.md).

- Repo: <https://github.com/obra/superpowers>
- Pinned at commit [`b36e082`](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797) · plugin version `6.3.0`
- All links below point at that commit on GitHub.

---

## How it lands on disk

`superpowers` is its **own marketplace**: the repo root holds both
[`.claude-plugin/plugin.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/.claude-plugin/plugin.json)
and
[`.claude-plugin/marketplace.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/.claude-plugin/marketplace.json),
and the marketplace entry is `"source": "./"` — the plugin **is the whole repo**.

```
/plugin marketplace add obra/superpowers
  → git clone → ~/.claude/plugins/marketplaces/superpowers-dev/

/plugin install superpowers@superpowers-dev
  → copies the ENTIRE repo tree (194 files, ~2.3 MB) to
    ~/.claude/plugins/cache/superpowers-dev/superpowers/6.3.0/
  → root package.json has no dependencies and no scripts, so the
    npm-install step is a no-op
  → enables the plugin in settings
```

**Consequence:** every file in the repo is copied to the user's machine —
`tests/`, `docs/`, `assets/`, the other-harness manifests (`.codex-plugin/`,
`.cursor-plugin/`, …), all of it. Only a subset is *active* in Claude Code.
"Installed" and "active" are different questions and the table below answers
both.

`version` is `6.3.0` in `plugin.json`; users get updates only when that field
is bumped (it is not pinned to a SHA in the marketplace entry).

---

## What Claude Code actually activates

`plugin.json` declares **no** component-path overrides, so Claude Code uses the
defaults and finds exactly:

| Component | Present? | Path |
|---|---|---|
| Manifest | ✅ | [`.claude-plugin/plugin.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/.claude-plugin/plugin.json) |
| Skills | ✅ 14 | [`skills/`](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills) |
| Hooks | ✅ 1 event | [`hooks/hooks.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/hooks.json) |
| `commands/` | ❌ | — |
| `agents/` | ❌ | — (the `*-prompt.md` files are skill-bundled text, not registered agents) |
| `.mcp.json` | ❌ | — (the brainstorm server is a skill-launched script, not a plugin MCP server) |
| `.lsp.json` | ❌ | — |
| `monitors/` | ❌ | — |
| `bin/` | ❌ | — |
| `settings.json` | ❌ | — |

So the **automatic** on-machine footprint is: the manifest (read) + one
`SessionStart` hook (**runs a bundled bash script on every session start**) +
14 skills (text loaded into the model; their bundled scripts run only if the
model is instructed to run them).

---

## Asset inventory + scan verdict

**Scan-type key:**

| Tag | Meaning |
|---|---|
| 🔴 **LLM scan** | Non-deterministic threat pass needed (new work for the plugin scanner) |
| 🟠 **LLM scan — code** | Runnable code; needs a code-threat pass (network, creds, obfuscation, shell-out) |
| 🟡 **reuse skill scan** | Same content/threats as a standalone skill — point the existing skill scanner at it |
| 🟢 **deterministic** | Schema / static-rule check, no model needed |
| ⚪ **no scan** | Docs / examples / fixtures — inert text, not loaded and not run |
| ⚫ **inert in Claude Code** | Installed to disk but never read or run by Claude Code (other-harness files) |

### 1. Manifest & marketplace

| Asset | Runs on machine? | Scan | Notes |
|---|---|---|---|
| [`.claude-plugin/plugin.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/.claude-plugin/plugin.json) | no (config) | 🟢 **deterministic** + light 🔴 | Schema; check no path override escapes the plugin dir; `description`/`keywords` lure check. This copy is clean (name, author, MIT, keywords). |
| [`.claude-plugin/marketplace.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/.claude-plugin/marketplace.json) | no (config) | 🟢 **deterministic** | `source: "./"`, no `strict:false`, no cross-marketplace deps. Clean. |

### 2. Hooks — the automatic execution surface

| Asset | Runs on machine? | Scan | Notes |
|---|---|---|---|
| [`hooks/hooks.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/hooks.json) | wiring | 🔴 **LLM scan** + 🟢 | One `SessionStart` hook, `matcher: "startup\|clear\|compact"`, `type: command`, runs `"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" session-start`. Deterministic flags: auto-trigger event ✔, `command` type ✔, no `http` ✔. LLM: judge the intent of what the command does. |
| [`hooks/run-hook.cmd`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/run-hook.cmd) | ✅ **every session start** | 🟠 **LLM scan — code** | cmd.exe/bash polyglot wrapper. On Windows: searches `C:\Program Files\Git\bin\bash.exe` etc., then `bash` on `PATH`, execs the named hook script; exits 0 silently if no bash. On Unix: `exec bash "${SCRIPT_DIR}/session-start"`. Behaviour: locate an interpreter and run a sibling script. Low risk here but this is exactly the "wrapper indirection" a scanner must follow through. |
| [`hooks/session-start`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/session-start) | ✅ **every session start** | 🟠 **LLM scan — code** + 🔴 | **The key asset.** Reads [`skills/using-superpowers/SKILL.md`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/using-superpowers/SKILL.md), JSON-escapes it, and emits it as `hookSpecificOutput.additionalContext` wrapped in `<EXTREMELY_IMPORTANT>You have superpowers…</EXTREMELY_IMPORTANT>`. Effect: **injects a skill's full text + a compliance directive into the model's context on every session, with no user action.** Also branches on `CURSOR_PLUGIN_ROOT` / `COPILOT_CLI` env to pick the output field. A scanner must flag context-injection-on-autotrigger and assess the injected payload. |
| [`hooks/hooks-cursor.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/hooks-cursor.json) | ⚫ inert in Claude Code | ⚫ | Cursor's hook format (`sessionStart` camelCase). Not read by Claude Code. |

### 3. Skills — text loaded into the model

All 14 are model-invoked skills. Content threats (indirect prompt injection,
policy violation, exfiltration instructions) are the **same as a standalone
skill**, so 🟡 **reuse the existing skill scanner** — but note two are higher
priority because a hook auto-injects one and one is a session-opener.

| Skill | Link | Scan | Notes |
|---|---|---|---|
| `using-superpowers` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/using-superpowers/SKILL.md) | 🟡 **reuse skill scan — priority** | Auto-injected every session by the hook. `<EXTREMELY-IMPORTANT>`, "YOU DO NOT HAVE A CHOICE. YOU MUST USE IT. This is not negotiable. You cannot rationalize your way out of this." Benign in intent (get the model to use skills) but this is the exact linguistic shape a malicious injected payload uses — scan it *as an injected payload*, not just as a skill. |
| `using-superpowers/references/*.md` (5) | [references/](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/using-superpowers/references) | 🟡 reuse skill scan | Per-harness tool lists (antigravity, codex, gemini, hermes, pi). Loaded on demand. |
| `brainstorming` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/SKILL.md) | 🟡 reuse skill scan | `<HARD-GATE>` blocking language; instructs the model to run the brainstorm server scripts (§4). |
| `brainstorming/visual-companion.md` | [visual-companion.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/visual-companion.md) | 🟡 reuse skill scan | Instructions for launching the local web UI. |
| `brainstorming/spec-document-reviewer-prompt.md` | [file](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/spec-document-reviewer-prompt.md) | 🟡 reuse skill scan | Subagent prompt template (text pasted into a dispatched subagent, not a registered agent). |
| `dispatching-parallel-agents` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/dispatching-parallel-agents/SKILL.md) | 🟡 reuse skill scan | |
| `executing-plans` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/executing-plans/SKILL.md) | 🟡 reuse skill scan | |
| `finishing-a-development-branch` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/finishing-a-development-branch/SKILL.md) | 🟡 reuse skill scan | Runs `git` integration commands. |
| `receiving-code-review` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/receiving-code-review/SKILL.md) | 🟡 reuse skill scan | |
| `requesting-code-review` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/requesting-code-review/SKILL.md) | 🟡 reuse skill scan | |
| `requesting-code-review/code-reviewer.md` | [file](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/requesting-code-review/code-reviewer.md) | 🟡 reuse skill scan | Subagent prompt template. |
| `subagent-driven-development` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/subagent-driven-development/SKILL.md) | 🟡 reuse skill scan | Instructs the model to run the `scripts/` shell tools (§4). |
| `subagent-driven-development/*-prompt.md` (4) | [dir](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/subagent-driven-development) | 🟡 reuse skill scan | `implementer-prompt.md`, `re-review-prompt.md`, `task-reviewer-prompt.md` + the one in `brainstorming` — subagent prompt templates. |
| `systematic-debugging` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/systematic-debugging/SKILL.md) | 🟡 reuse skill scan | |
| `systematic-debugging/*.md` (support docs) | [dir](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/systematic-debugging) | ⚪ / 🟡 | `condition-based-waiting.md`, `defense-in-depth.md`, `root-cause-tracing.md` — reference docs. `test-pressure-1/2/3.md`, `test-academic.md`, `CREATION-LOG.md` — eval fixtures for testing the skill; inert. |
| `test-driven-development` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/test-driven-development/SKILL.md) | 🟡 reuse skill scan | |
| `test-driven-development/writing-good-tests.md` | [file](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/test-driven-development/writing-good-tests.md) | ⚪ no scan | Reference doc. |
| `using-git-worktrees` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/using-git-worktrees/SKILL.md) | 🟡 reuse skill scan | Runs `git worktree` commands. |
| `verification-before-completion` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/verification-before-completion/SKILL.md) | 🟡 reuse skill scan | |
| `writing-plans` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/writing-plans/SKILL.md) | 🟡 reuse skill scan | |
| `writing-plans/plan-document-reviewer-prompt.md` | [file](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/writing-plans/plan-document-reviewer-prompt.md) | 🟡 reuse skill scan | Subagent prompt template. |
| `writing-skills` | [SKILL.md](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/writing-skills/SKILL.md) | 🟡 reuse skill scan | |
| `writing-skills/persuasion-principles.md` | [file](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/writing-skills/persuasion-principles.md) | 🟡 **reuse skill scan — note** | Explicitly teaches writing more coercive skill language ("persuasion principles"). Benign as authoring guidance; worth a scanner note because it is instructions for making instructions harder to ignore. |
| `writing-skills/anthropic-best-practices.md`, `testing-skills-with-subagents.md`, `graphviz-conventions.dot`, `examples/CLAUDE_MD_TESTING.md` | [dir](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/writing-skills) | ⚪ no scan | Reference docs + a graphviz style file + an example. |

### 4. Bundled scripts — run only when a skill tells the model to

Not auto-run. But they are on disk and execute with the user's full privileges
the moment the model follows the skill that invokes them, so each needs a
code-threat pass.

| Asset | Invoked by | Scan | What it does |
|---|---|---|---|
| [`skills/brainstorming/scripts/server.cjs`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/scripts/server.cjs) | `brainstorming` (visual companion) | 🟠 **LLM scan — code — priority** | 723-line zero-dependency Node **HTTP + WebSocket server** (hand-rolled RFC 6455). Binds `127.0.0.1` on a random high port by default (`BRAINSTORM_HOST` env can override to `0.0.0.0`). Has a token-auth scheme and a WebSocket Origin allowlist. **Launches the user's browser** via `child_process.execFile` (platform launcher: `open` / `xdg-open` / `cmd /c start`) and via `cp.exec(process.env.BRAINSTORM_OPEN_CMD + ' ' + url)` when that env var is set (shell invocation). Serves local files. This is the single largest and most security-relevant runnable asset in the plugin. |
| [`skills/brainstorming/scripts/start-server.sh`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/scripts/start-server.sh) | `brainstorming` | 🟠 LLM scan — code | 209 lines. Backgrounds `server.cjs`, writes PID/state under `/tmp` or `<project>/.superpowers/brainstorm/`, supports `--host 0.0.0.0`, `--open` (auto-open browser), `--idle-timeout-minutes`. |
| [`skills/brainstorming/scripts/stop-server.sh`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/scripts/stop-server.sh) | `brainstorming` | 🟠 LLM scan — code | 120 lines. Verifies a server-instance-id, kills the PID, deletes the session dir only if under `/tmp`. |
| [`skills/brainstorming/scripts/helper.js`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/scripts/helper.js) | served to the browser | 🟠 LLM scan — code | 167-line browser-side WebSocket reconnect client. Runs in the companion web page, not in Node. |
| [`skills/brainstorming/scripts/frame-template.html`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/scripts/frame-template.html) | served to the browser | 🟢 deterministic | Static HTML shell. Check for outbound resource loads / inline script. |
| [`skills/subagent-driven-development/scripts/sdd-workspace`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/subagent-driven-development/scripts/sdd-workspace) | `subagent-driven-development` | 🟠 LLM scan — code | 40-line bash. `git rev-parse --show-toplevel`, `mkdir -p <root>/.superpowers/sdd/<slug>`, writes a self-ignoring `.gitignore`, prints the path. Writes inside the user's repo working tree. |
| [`skills/subagent-driven-development/scripts/task-brief`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/subagent-driven-development/scripts/task-brief) | same | 🟠 LLM scan — code | 41-line bash + awk. Extracts one task's text from a plan file into the workspace. |
| [`skills/subagent-driven-development/scripts/review-package`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/subagent-driven-development/scripts/review-package) | same | 🟠 LLM scan — code | 46-line bash. `git log` / `git diff` between two refs into a `.diff` file. |
| [`skills/systematic-debugging/find-polluter.sh`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/systematic-debugging/find-polluter.sh) | `systematic-debugging` | 🟠 LLM scan — code | 72-line bash. Bisects a test suite to find which test creates a file; runs arbitrary test commands the caller supplies. |
| [`skills/systematic-debugging/condition-based-waiting-example.ts`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/systematic-debugging/condition-based-waiting-example.ts) | example only | ⚪ no scan | TypeScript code sample in a doc; not executed. |
| [`skills/writing-skills/render-graphs.js`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/writing-skills/render-graphs.js) | `writing-skills` | 🟠 LLM scan — code | 169-line Node. Extracts ` ```dot ` blocks from a `SKILL.md` and shells out to `dot` (graphviz) via `execFileSync` to render SVGs. |

### 5. Installed but never touched by Claude Code

Copied into the cache by the `source: "./"` install, but not read or run by
Claude Code. Relevant to a scanner only as "why is this in the payload" noise —
and because a malicious plugin could hide a real payload among files like these.

| Asset | Link | Scan | Notes |
|---|---|---|---|
| `.codex-plugin/`, `.cursor-plugin/`, `.devin-plugin/`, `.kimi-plugin/`, `.hermes-plugin/`, `.opencode/`, `.pi/`, `.agents/`, `gemini-extension.json`, `GEMINI.md` | [repo root](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797) | ⚫ inert in Claude Code | Manifests/entrypoints for other agent harnesses (Codex, Cursor, Devin, Kimi, Hermes, opencode, pi, Gemini). `.opencode/plugins/superpowers.js`, `.pi/extensions/superpowers.ts`, `.hermes-plugin/__init__.py` are **executable code for those platforms** — inert under Claude Code but a scanner should confirm that and report their presence. |
| [`tests/`](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/tests) (≈70 files, incl. `tests/brainstorm-server/package.json` + `package-lock.json`) | | ⚪ no scan | Test suite for the plugin's own scripts. Not run on install (not at plugin root, so no `npm install`). `analyze-token-usage.py` and many `.sh` — inert unless a user runs the suite manually. |
| [`docs/`](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/docs) (≈40 plan/spec markdown files) | | ⚪ no scan | Design docs and historical plans. |
| [`scripts/`](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/scripts) — `bump-version.sh`, `lint-shell.sh`, `package-codex-plugin.sh`, `sync-to-codex-plugin.sh` | | ⚪ no scan | Repo maintenance scripts. Not referenced by any skill or hook. Inert unless run by hand. |
| [`assets/`](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/assets) — `app-icon.png`, `superpowers-small.svg` | | 🟢 deterministic | Images. SVG can carry script — a static check is enough. |
| `CLAUDE.md`, `AGENTS.md` (symlink → `CLAUDE.md`), `README.md`, `RELEASE-NOTES.md`, `LICENSE`, `CODE_OF_CONDUCT.md`, `.github/`, `.pre-commit-config.yaml`, `.gitattributes`, `.gitignore`, `.version-bump.json`, root `package.json` | [repo root](https://github.com/obra/superpowers/tree/b36e0829c6d0140e93cfef2ca599b1b07d4a7797) | ⚪ no scan / 🟢 | Project-level files. Claude Code does **not** load a plugin's `CLAUDE.md`. Root `package.json` — 🟢 deterministic: confirm no `scripts` (it has none) so the install-time `npm install` does nothing. |

---

## Summary — what needs a security scan

**Automatic execution (highest priority — runs with zero user action):**

1. [`hooks/session-start`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/session-start) — code pass + injected-payload pass. Injects [`using-superpowers/SKILL.md`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/using-superpowers/SKILL.md) + a compliance directive into the model every session.
2. [`hooks/run-hook.cmd`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/run-hook.cmd) — code pass (interpreter-discovery wrapper).
3. [`hooks/hooks.json`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/hooks/hooks.json) — wiring pass + deterministic flags.

**Runnable on skill invocation (code-threat pass each):**

4. [`skills/brainstorming/scripts/server.cjs`](https://github.com/obra/superpowers/blob/b36e0829c6d0140e93cfef2ca599b1b07d4a7797/skills/brainstorming/scripts/server.cjs) — local HTTP/WS server + browser launch + shell-out. **Largest single risk asset.**
5. `start-server.sh`, `stop-server.sh`, `helper.js` (brainstorm server support).
6. `subagent-driven-development/scripts/{sdd-workspace,task-brief,review-package}` — write into the user's repo tree, run `git`.
7. `systematic-debugging/find-polluter.sh` — runs arbitrary caller-supplied test commands.
8. `writing-skills/render-graphs.js` — shells out to `dot`.

**Model-context (reuse the existing skill scanner):**

9. All 14 skills + their bundled `.md` (SKILL text, subagent prompt templates, reference docs). Priority: `using-superpowers` (auto-injected) and `brainstorming` (launches the server). Note `persuasion-principles.md`.

**Deterministic only:**

10. `plugin.json`, `marketplace.json`, `frame-template.html`, `assets/*.svg`, root `package.json`.

**No scan needed:** `docs/`, `tests/`, `scripts/`, eval fixtures under
`systematic-debugging/`, code examples, project metadata.

**Inert under Claude Code (report presence, don't scan as active):** all
`.{codex,cursor,devin,kimi,hermes}-plugin/` + `.opencode/` + `.pi/` +
`.agents/` + `gemini-extension.json` + `GEMINI.md`.

### Counts

| Verdict | Approx. asset count |
|---|---|
| 🔴 / 🟠 needs LLM (wiring or code) | ~11 (3 hook, 8 script) |
| 🟡 reuse skill scan | 14 skills + ~10 bundled `.md` |
| 🟢 deterministic | ~5 |
| ⚪ no scan | ~90 (docs, tests, fixtures, metadata) |
| ⚫ inert in Claude Code | ~15 (other-harness files) |
| **Total installed** | **194 files** |
