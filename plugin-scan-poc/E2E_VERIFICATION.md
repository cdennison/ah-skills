# E2E verification — does `discover_assets.py` find everything a plugin install writes to disk?

The scanner's job is to hand a threat scanner **every file that reaches a user's
machine** when a plugin is installed. The only way to be sure it does is to
*actually install the plugin* and diff the result. This is that check.

## Principle

> Understand what files are copied to a user's computer when a plugin is
> installed. From that, reverse-engineer what is an asset that might need
> scanning.

`discover_assets.py` models **install surfaces** (a marketplace entry's `source`
dir, a `.claude-plugin/plugin.json` dir, a `.claude/`/`.cursor/`/… standalone
dir). The e2e test confirms those models against `claude plugin install`.

## How it runs

```
e2e/
├── Dockerfile          # node:22-slim + claude-code CLI + python3; CLAUDE_CONFIG_DIR=/claude-home
├── verify.sh           # (in container) install every plugin every marketplace.json declares;
│                       #   record the exact installed file tree + `claude plugin details`
├── compare_install.py  # (host) diff installed tree vs discover_assets.py catalogue
└── run_e2e.sh          # (host) build image → docker run --rm → discover → compare
```

```bash
plugin-scan-poc/e2e/run_e2e.sh repos/superpowers repos/impeccable
```

**Isolation / cleanup.** The repo is mounted read-only. The sandbox's Claude
config only ever exists at a container path (`CLAUDE_CONFIG_DIR=/claude-home`),
so `docker run --rm` is a complete teardown — the host `~/.claude` is never
touched. `run_e2e.sh --rmi` also deletes the image. `e2e/out/` (the install
snapshots) is gitignored.

A lighter host-only variant (no Docker) works too:
`CLAUDE_CONFIG_DIR=$(mktemp -d) claude plugin marketplace add <repo> && claude plugin install <p>@<mkt>`
— then delete the temp dir. Docker is preferred because a plugin's bundled
`package.json` triggers `npm install` on the host otherwise.

## What "PASS" means

Every file in the plugin's install cache dir is either

* a **catalogued asset** in `discover_assets.py`'s output, or
* an **explicit exclusion** with a stated reason (lockfile, image, `.gitignore`, …)

Zero files "installed but not accounted for". Plus: `claude plugin details`'s
component counts (skills / agents / hooks / mcp) reconcile with the catalogue
for that surface.

## Results — 2026-09-05

`claude` 2.1.261. Both repos: **PASS**.

### `obra/superpowers` — marketplace `source: "./"`

| | value |
|---|---|
| files copied to `~/.claude/plugins/cache/superpowers-dev/superpowers/6.3.0/` | **194** (the entire repo — `tests/`, `docs/`, `.hermes-plugin/`, every other-harness manifest, all of it) |
| catalogued assets (repo) | 177 |
| explicit exclusions | 18 (17 inert + `AGENTS.md` symlink) |
| **installed, not accounted for** | **0** |
| `claude plugin details` | Skills **14** · Agents **0** · Hooks **1** (SessionStart) · MCP **0** · LSP **0** |
| discover, same surface | Skills 14 ✓ · Agents 0 ✓ · Hook files 2 (the Claude + Cursor `hooks*.json`), 2 hook entries · MCP 0 ✓ |

Confirms: a `source: "./"` plugin ships the **whole repo**, exactly as
`discover_assets.py` assumes (surface root = repo root, 194 files).

### `pbakaus/impeccable` — marketplace `source: "./plugin"`

| | value |
|---|---|
| repo size | 2 956 files (Rust workspace + browser extension + npm CLI + 18 per-harness skill copies + the plugin) |
| files copied to `~/.claude/plugins/cache/impeccable/impeccable/4.2.0/` | **58** — only `plugin/**` |
| discover `marketplace:impeccable/impeccable` surface | **58**, same files |
| **installed, not accounted for** | **0** |
| `claude plugin details` | Skills **1** · Agents **4** · Hooks **2** (PostToolUse, Stop) · MCP **0** |
| discover, same surface | Skills 1 ✓ · Agents 4 ✓ · 1 hook file wiring 2 entries ✓ · MCP 0 ✓ |

Confirms: a subdir `source` ships **only that subdir**. `discover_assets.py`
resolves the marketplace `source` and scopes the surface to `plugin/`; the
other 2 898 files (crates, extension, tests, the per-harness copies) are
correctly **not** part of *this* plugin's install — they are reported as
orphans + a `related_out_of_plugin_scope` list (browser extension, rust engine,
npm CLI wrapper, plugin build pipeline), each to be scanned as its own asset
type.

## Bugs this test caught (and fixed)

| Found by | Bug | Fix |
|---|---|---|
| impeccable diff | `compare_install.py` compared cache-relative paths (`skills/…`) against repo-relative catalogue paths (`plugin/skills/…`) → 57 false "MISS" | map installed paths onto the surface's `source` root before diffing |
| impeccable, v0.3 | no surface model → the whole 2 956-file monorepo catalogued as "assets" (1 764 of them) | v0.4 install-surface model; monorepo files outside `plugin/` become orphans |
| impeccable hooks | `${CLAUDE_PLUGIN_ROOT}` in `plugin/hooks/hooks.json` resolved against the manifest's own dir, not the plugin root → the `impeccable` launcher script was missed | resolve `${CLAUDE_PLUGIN_ROOT}` / relative paths against every owning surface root |
| impeccable `.github/hooks` | `$(git rev-parse --show-toplevel)/…` mangled by the tokenizer → false blind-spot | strip `$(…)` spans before tokenizing |
| superpowers `session-start` | hook command names a wrapper (`run-hook.cmd session-start`) with the real script as a bare arg | wrapper-arg heuristic: check for a sibling file named by each bare token |

## Standing blind spots (real, not bugs)

* `.codex/hooks.json` (impeccable) points its command at
  `.codex/skills/impeccable/scripts/impeccable`, which **is not in the repo** —
  the `.codex/` surface ships only `hooks.json`. Either another tool populates
  it at runtime or it is a dead path. A scanner must still record that this
  hook *would* run something. `discover_assets.py` emits this in `blind_spots`.
