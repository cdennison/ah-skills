# Manually added repos (historical -- superseded by registry.json)

**This file is no longer read by the pipeline.** `clone_repos.py` reads
`repo-seeds/registry.json` exclusively; that's the single source of truth
for every repo, tagged with where it came from (`seed` / `search` / `manual`)
and the provenance detail for each. See `../registry.py` for the curation
API/CLI and `../docs/ARCHITECTURE.md` for the full flow.

The repos that used to live in this file were migrated into `registry.json`
by `../build_registry.py` (one-time migration, already run). Kept here only
as a historical record of what this file used to contain -- do not add new
repos here; use `./registry.py add-manual owner/repo "reason"` or
`./registry.py add-search results.json --approve owner/repo` instead.

## Hand-picked (migrated to registry.json, source="manual")

- [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills)
- [sickn33/agentic-awesome-skills](https://github.com/sickn33/agentic-awesome-skills/tree/main)
- [microsoft/skills](https://github.com/microsoft/skills)
- [Shubhamsaboo/awesome-llm-apps](https://github.com/Shubhamsaboo/awesome-llm-apps)

## From GitHub search (migrated to registry.json, source="search")

- [agentskills/agentskills](https://github.com/agentskills/agentskills)
- [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)
