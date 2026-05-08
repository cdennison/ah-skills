# Seed Skill Directories

Annotated list of canonical Claude Skill directories. The seed loader (`scripts/seed_registry.py`) parses the YAML blocks below to bootstrap `registry/skill-directories.yml` on first run. Update this file when a directory becomes canonical (long-running, widely cited) so future cold starts capture it.

Each entry is a fenced YAML block tagged `seed`. The loader skips everything outside those blocks, so prose and headings are free to use.

---

## Tier A — Anthropic / open-standard

```yaml seed
name: anthropics-skills
url: https://github.com/anthropics/skills
kind: collection
ref: main
scan_paths:
  - skills/*/SKILL.md
description: Anthropic's official Agent Skills repository, including the spec, the skill-creator template, and the production document skills (docx, pdf, pptx, xlsx).
license: Apache-2.0 / source-available
tags: [official, anthropic, reference]
```

```yaml seed
name: anthropics-claude-plugins-official
url: https://github.com/anthropics/claude-plugins-official
kind: collection
ref: main
scan_paths:
  - plugins/*/skills/*/SKILL.md
  - skills/*/SKILL.md
description: Anthropic-managed directory of high-quality Claude Code plugins, several of which bundle skills.
license: Apache-2.0
tags: [official, anthropic, plugins]
```

```yaml seed
name: agentskills-io
url: https://agentskills.io
kind: registry
ref: ""
scan_paths: []
requires_crawler: true
description: Home of the Agent Skills open standard. Aggregates skills across multiple agent products (Claude Code, Codex, Cursor, Gemini CLI).
license: spec is open
tags: [spec, registry, cross-tool]
```

## Tier B — Curated awesome-lists

```yaml seed
name: composiohq-awesome-claude-skills
url: https://github.com/ComposioHQ/awesome-claude-skills
kind: awesome-list
ref: main
scan_paths: []
requires_crawler: true
description: Composio's curated list of Claude Skills, focused on automation agents.
license: MIT
tags: [community, awesome-list, automation]
```

```yaml seed
name: travisvn-awesome-claude-skills
url: https://github.com/travisvn/awesome-claude-skills
kind: awesome-list
ref: main
scan_paths: []
requires_crawler: true
description: Travis Fischer's awesome-list of Claude Skills, particularly Claude Code.
license: CC-BY-4.0
tags: [community, awesome-list]
```

```yaml seed
name: voltagent-awesome-agent-skills
url: https://github.com/VoltAgent/awesome-agent-skills
kind: awesome-list
ref: main
scan_paths: []
requires_crawler: true
description: 1000+ agent skills from official dev teams (Anthropic, Google Labs, Vercel, Stripe, Cloudflare, Netlify, Trail of Bits, Sentry, Expo, Hugging Face, Figma) and the community.
license: MIT
tags: [community, awesome-list, 1k+, vendor-aggregated]
```

```yaml seed
name: sickn33-antigravity-awesome-skills
url: https://github.com/sickn33/antigravity-awesome-skills
kind: awesome-list
ref: main
scan_paths: []
requires_crawler: true
description: Installable library of 1,400+ agentic skills for Claude Code, Cursor, Codex CLI, Gemini CLI, Antigravity, and more. Ships an installer CLI.
license: MIT
tags: [community, awesome-list, 1k+, cross-tool]
```

```yaml seed
name: behisecc-awesome-claude-skills
url: https://github.com/BehiSecc/awesome-claude-skills
kind: awesome-list
ref: main
scan_paths: []
requires_crawler: true
description: Curated list of Claude Skills.
tags: [community, awesome-list]
```

```yaml seed
name: glebis-claude-skills
url: https://github.com/glebis/claude-skills
kind: collection
ref: main
scan_paths:
  - skills/*/SKILL.md
description: Gleb's collection of Claude Code skills for AI workflows.
tags: [community]
```

```yaml seed
name: daymade-claude-code-skills
url: https://github.com/daymade/claude-code-skills
kind: collection
ref: main
scan_paths:
  - skills/*/SKILL.md
description: Production-ready Claude Code skills for development workflows.
tags: [community]
```

## Tier C — Marketplaces and registries

```yaml seed
name: claudemarketplaces-com
url: https://claudemarketplaces.com
kind: marketplace
ref: ""
scan_paths: []
requires_crawler: true
description: Directory of Claude Code plugins, skills, and MCP servers with community voting.
tags: [marketplace, hosted]
```

```yaml seed
name: buildwithclaude-com
url: https://buildwithclaude.com
kind: marketplace
ref: ""
scan_paths: []
requires_crawler: true
description: Plugin marketplace for Claude Code.
tags: [marketplace, hosted]
```

```yaml seed
name: claude-plugins-dev
url: https://claude-plugins.dev
kind: registry
ref: ""
scan_paths: []
requires_crawler: true
description: Community registry of Claude Code plugins and Agent Skills with a CLI installer.
tags: [registry, hosted, cli]
```

```yaml seed
name: skills-sh
url: https://skills.sh
kind: registry
ref: ""
scan_paths: []
requires_crawler: true
description: The Agent Skills Directory.
tags: [registry, hosted]
```

```yaml seed
name: majiayu000-claude-skill-registry
url: https://github.com/majiayu000/claude-skill-registry
kind: registry
ref: main
scan_paths: []
requires_crawler: true
description: Comprehensive Claude Code skills registry, updated daily, with a web frontend at skills-registry-web.vercel.app.
tags: [registry, large]
```

```yaml seed
name: lap-platform-claude-marketplace
url: https://github.com/Lap-Platform/claude-marketplace
kind: marketplace
ref: main
scan_paths:
  - marketplace/*/skills/*/SKILL.md
description: 1,500+ auto-generated API skills (Payments, Dev tools, Productivity, AI/ML, Communication).
tags: [marketplace, large, api-generated]
```

```yaml seed
name: awesomeclaude-ai
url: https://awesomeclaude.ai/awesome-claude-skills
kind: registry
ref: ""
scan_paths: []
requires_crawler: true
description: Visual directory of awesome Claude Skills.
tags: [registry, hosted, visual]
```

---

## Vendor candidates to investigate

These are mentioned in `voltagent-awesome-agent-skills` but the vetters should confirm canonical first-party URLs before adding them as standalone entries (rather than relying on the awesome-list aggregation):

- Vercel
- Stripe
- Cloudflare
- Netlify
- Trail of Bits
- Sentry
- Expo
- Hugging Face
- Figma
- Google Labs
- Replicate

Suggested scout query for these: `mcp__github__search_code` with `path:skills filename:SKILL.md org:<vendor>`.
