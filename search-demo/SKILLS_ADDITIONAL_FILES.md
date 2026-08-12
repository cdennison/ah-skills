# Additional files around SKILL.md that signal target agent

Findings from analyzing `tdd-workflow` across all 9 mirrored copies in
`test-data/ECC` (affaan-m/ECC). Goal: catalog every non-`SKILL.md` file that
touched the skill, and classify each as either (a) evidence of which agent
the skill targets, or (b) something that looks related but isn't — with an
explicit split between files that **control runtime behavior** (the agent
actually reads them to decide what to do) and files that are **install/build
scaffolding or pure documentation** (help a human or installer move files
around, but the agent never loads them at skill-execution time).

Paths below are relative to the repo root (`test-data/ECC/`).

---

## 1. Files that control runtime behavior

These are read by the agent (or its plugin loader) while the skill is
active. They are real, high-confidence signal.

### `.claude-plugin/plugin.json`
- **Target:** Claude Code
- **Runtime role:** Claude Code's plugin loader reads this to know what a
  plugin ships. `"skills": ["./skills/"]` tells Claude Code to load every
  `SKILL.md` under `skills/` as an installed skill. This is the mechanism —
  not just documentation of intent.
- **Consequence:** any skill under `skills/` (plain, no dot-prefix) is
  claimed by Claude Code *because this file says so*, not because of the
  path string alone.

### `.codex-plugin/plugin.json`
- **Target:** Codex
- **Runtime role:** same mechanism as above, for Codex's plugin loader.
  `"skills": "./skills/"` — **points at the same physical directory** as
  the Claude manifest. Also carries Codex-specific runtime fields:
  `interface.defaultPrompt`, `interface.capabilities`, `composerIcon` —
  these actively shape how Codex presents/invokes the plugin, not just
  metadata.
- **Consequence:** `skills/tdd-workflow/SKILL.md` is dual-targeted
  (`claude-code` + `codex`) via two independent manifests, not "unknown."

### `<skill-dir>/agents/*.md` and `*.sh` — NOT the same convention as the sidecar above
- Example: `skills/lead-intelligence/agents/{enrichment-agent,mutual-mapper,outreach-drafter,signal-scorer}.md`,
  `skills/continuous-learning-v2/agents/{observer,session-guardian,start-observer}.sh`
- **Not agent-target signal.** Despite the identical directory name
  (`agents/`), these are **subagent definitions the skill itself invokes**
  (Claude Code subagents / helper scripts), not per-target-agent config.
  The distinguishing feature is the file extension: `.yaml`/`.yml` under
  `agents/` = target-agent sidecar (see above); `.md`/`.sh` under
  `agents/` = the skill's own subagents. Verified against all 282 curated
  skills in ECC — every `agents/` dir under `skills/` holds `.md`/`.sh`
  files, zero hold `.yaml`, so this never collides with the sidecar
  convention in practice, but a classifier must still check the extension
  rather than just the directory name.

### `<skill-dir>/agents/<agent-name>.yaml`
- Example: `.agents/skills/tdd-workflow/agents/openai.yaml`
- **Target:** whatever `<agent-name>` names (here: `openai`)
- **Runtime role:** a per-skill, per-agent override consumed by that
  agent's runtime when it loads this specific skill — `interface.
  display_name`, `brand_color`, `default_prompt`, and
  `policy.allow_implicit_invocation` (whether the agent may invoke the
  skill without explicit user request) are all behavior-affecting, not
  descriptive.
- **This is the single strongest, most granular signal available.** It's
  scoped to one skill and one agent, so it should override/add to
  whatever a broader repo-level manifest or path convention implies.

### `.claude-plugin/marketplace.json`
- **Target:** Claude Code (as the marketplace/distribution mechanism)
- **Runtime role:** consumed by Claude Code's `/plugin marketplace`
  install flow, not by the running agent per se — but it's still
  functional (drives what gets installed), not just prose. Its
  free-text `description` mentioning "Claude Code, Codex, OpenCode,
  Cursor" is *documentation*, not itself a mechanism — treat that
  sentence as a hint, not a manifest signal.

### `manifests/install-modules.json`
- **Target:** ambiguous — repo-internal, not agent-specific
- **Runtime role:** consumed by this repo's *own* installer tooling
  (`scripts/*`) to decide what gets copied where during `install`. It
  confirms a skill is "curated" (see `docs/SKILL-PLACEMENT-POLICY.md`)
  but doesn't by itself say which agent — use it as a corroborating
  signal that `skills/tdd-workflow` is the canonical/shipped copy, not
  as an agent-identity signal on its own.

---

## 2. Files that look like agent signal but are install/build scaffolding

These exist *because of* a target agent (someone built tooling to support
that agent), but the agent itself never reads them at skill-runtime — a
human or a shell script runs them once, ahead of time, to copy files into
place. Conflating these with runtime manifests will overcount "how many
agents actively support this skill."

### `.kiro/install.sh`, `.kiro/README.md`
- **Target:** Kiro (indirectly)
- **Role:** a *build step*, not a runtime file. `install.sh` is a bash
  script a human runs (`./install.sh /path/to/project`) that copies
  `.kiro/skills/*`, `.kiro/agents/*`, `.kiro/steering/*` into a target
  Kiro project. Kiro's actual runtime only ever sees the *copied*
  `SKILL.md` files post-install — it never executes or reads
  `install.sh` itself.
- **Why it still counts as evidence:** the presence of a whole parallel
  `.kiro/` tree (its own `agents/`, `skills/`, `steering/`, `hooks/`,
  `settings/`) is strong *structural* evidence that this repo maintains
  a dedicated Kiro distribution — just don't treat `install.sh` as a
  "runtime config file" the way `agents/openai.yaml` is.

### `.kiro/skills/tdd-workflow/SKILL.md` frontmatter: `metadata.origin`, `metadata.version`
- **Target:** none — commonly misread as agent signal, isn't
- **Role:** `origin: ECC` is attribution (per
  `docs/SKILL-PLACEMENT-POLICY.md`: "Use `origin` in SKILL.md frontmatter
  \[...\] for attribution", distinguishing curated-by-ECC vs
  community-contributed). `version` is just the skill's own semver. Ignore
  both when classifying target agent.

### `docs/{es,zh-CN,zh-TW,ja-JP,tr,ko-KR}/skills/tdd-workflow/SKILL.md`
- **Target:** none — human-readable documentation mirrors
- **Role:** translated copies for people reading docs on GitHub. No
  install manifest references `docs/`, and no agent loader points at it.
  Should roll up to whatever `skills/tdd-workflow` (the canonical copy)
  resolves to, not be classified independently as its own signal source.

---

## 3. Files that exist but are not skill-specific at all

### `agent.yaml` (repo root)
- A `gitagent`-spec (`spec_version: "0.1.0"`) manifest exporting the
  *entire* skill/command catalog (all 281 skills, 94 commands) plus a
  preferred model (`claude-opus-4-6`). It's a whole-repo export surface,
  not scoped to `tdd-workflow` — useful for confirming the repo's overall
  center of gravity (Claude-first), useless for distinguishing one skill
  from another within the same repo.

### `CLAUDE.md`, `.claude/rules/*.md`
- Project-level guidance auto-loaded by Claude Code for *any* work in
  this repo (coding conventions, commit style, guardrails). Real runtime
  behavior for Claude Code specifically, but again not skill-scoped —
  applies uniformly regardless of which skill is active.

### `docs/SKILL-PLACEMENT-POLICY.md`
- Pure documentation of the repo's own skill-authoring conventions
  (curated vs learned vs imported vs evolved, `.provenance.json` schema
  for learned/imported skills). No runtime role at all. Useful only as a
  reference for what *would* count as provenance metadata (`source`,
  `created_at`, `confidence`, `author`) if `tdd-workflow` were a learned
  or imported skill — it isn't, so no `.provenance.json` exists here.

---

## 4. OpenClaw and Hermes — corpus-scale signals (no local checkout)

`tdd-workflow`/ECC was analyzed with a real clone on disk, so the classifier
could inspect plugin manifests and sidecar files directly. Most rows in
`skills_export.csv`/`skills_export_top.csv` don't have a local checkout —
only the CSV's `path`, `name`, `description`, `owner`, `repo` columns are
available. Grepping the CSV for `openclaw`/`hermes` surfaced two more
target-agent conventions, both usable at that reduced signal level:

### `.openclaw/skills/<name>/SKILL.md` — path token, same shape as `.kiro/`
- Example: `Green-PT/honey-for-devs/.openclaw/skills/honey/SKILL.md`
- **Runtime role:** repo-root `.openclaw/skills/` is OpenClaw's install
  location convention (14 skills found under it in that one repo alone —
  `honey`, `honey-review`, `honey-px`, `honey-memory`, `honey-loop`,
  `release-guard`, etc.). No plugin manifest was found alongside it in this
  corpus (unlike Claude/Codex's `.<agent>-plugin/plugin.json`), so treat it
  as **medium confidence**, same tier as `.kiro/`.

### "Hermes Agent" — text mention, no path convention found
- Example: `NeverSight/learn-skills.dev/data/skills-md/amanning3390/hermeshub/agent-hardening/SKILL.md`,
  description: *"Comprehensive security hardening for Hermes Agent..."*
- **Runtime role:** none discoverable from the path — this row is itself a
  mirrored copy inside a corpus-aggregator repo (`NeverSight/learn-skills.dev`,
  see §5), so the path signal points at the aggregator, not at Hermes. The
  only real signal is the description explicitly naming "Hermes Agent".
- **False-positive risk:** "hermes" alone is too generic (Greek god,
  shipping/fashion brand, unrelated software named Hermes) to use as a bare
  keyword. Matched only on multi-word phrases that specifically name the
  ecosystem: `"hermes agent"`, `"hermes-agent"`, `"hermeshub"`,
  `"oh-my-hermes"`, `"hermes skill"` — all seen repeatedly in the CSV grep
  (`hermes-agent` ×101, `oh-my-hermes` ×20, `hermeshub` in nested paths,
  etc.). **Confidence: low** — text mention is the weakest tier by design.

---

## 5. Junk aggregator repos — a different problem from agent targeting

`NeverSight/learn-skills.dev` (the Hermes example above lives inside it)
turned out to be a repo whose entire content is *other people's* skills
copied verbatim into `data/skills-md/<original-owner>/<original-repo>/...` —
a scrape/aggregation dump, not an original source. This isn't an
agent-target signal problem, it's a **corpus-quality** problem: every skill
inside it is a duplicate of a skill that (usually) already exists at its
real source path elsewhere in the dataset, and the aggregator's own
description/README text adds noise (e.g. re-describing skills in terms of
whatever agent the aggregator itself markets to) without adding a real
signal of its own.

**Heuristic to flag one:** a repo where most/all top-level skill paths
follow a `<data-dir>/<contributor-or-owner>/<original-repo>/SKILL.md`
nesting pattern (an extra owner/repo pair embedded *inside* the path, past
the real `owner/repo` the CSV row is already scoped to) is very likely a
scrape dump rather than a source repo. Separate from — and should run
*before* — agent-target classification, since classifying a copy inside a
junk aggregator as "hermes" (from the description) would be technically
correct for that one row but still wrong to index, given the same skill's
real source almost certainly already exists elsewhere in the corpus with a
cleaner path.

---

## Summary table

| File | Scope | Controls runtime? | Agent signal |
|---|---|---|---|
| `.claude-plugin/plugin.json` | repo | yes | claude-code |
| `.codex-plugin/plugin.json` | repo | yes | codex |
| `<skill>/agents/<name>.yaml` | per-skill, per-agent | yes | `<name>` (strongest, most granular) |
| `.claude-plugin/marketplace.json` | repo | partially (install flow) | claude-code (+ prose mentions others) |
| `manifests/install-modules.json` | repo | no (build-time only) | none directly; confirms "curated/shipped" |
| `.kiro/install.sh` | repo (Kiro tree) | no (one-time copy script) | kiro (structural, not runtime) |
| `.kiro/*/SKILL.md` `metadata.origin`/`version` | per-skill | n/a | none (attribution/versioning only) |
| `docs/{lang}/skills/*/SKILL.md` | per-skill | no (docs mirror) | none (rolls up to canonical copy) |
| `agent.yaml` (root) | whole repo | yes, but whole-catalog | weak/repo-wide only |
| `CLAUDE.md`, `.claude/rules/*` | whole repo | yes (Claude Code) | claude-code, but not skill-scoped |
| `docs/SKILL-PLACEMENT-POLICY.md` | whole repo | no | none |
| `.openclaw/skills/*/SKILL.md` path | per-skill | unknown (no manifest found) | openclaw (medium, path token) |
| "Hermes Agent" / "hermeshub" in description | per-skill | no | hermes (low, text mention only) |
| `data/skills-md/<owner>/<repo>/...` nested-path pattern | whole repo | no | none — flags a junk aggregator repo, not an agent |

**Practical rule of thumb:** trust files an agent's own loader reads
(plugin manifests, per-skill `agents/*.yaml`) over files a human runs once
(install scripts) over files that just talk about agents in prose
(descriptions, READMEs).
