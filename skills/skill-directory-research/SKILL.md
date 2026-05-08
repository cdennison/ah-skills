---
name: skill-directory-research
description: Spawn a research team that scouts the web for new Claude Skill directories (GitHub awesome-lists, plugin marketplaces, registries, vendor skill collections), vets each candidate, and registers approved entries in registry/skill-directories.yml so the agentic highway scanner can ingest them. Use when the user asks to grow the skill registry, scout for new skill repos or marketplaces, refresh known sources, audit which directories the highway is scanning, or rebuild the scan manifest.
when_to_use: Trigger on phrases like "find new skill directories", "scout for skills", "refresh the registry", "what should the agentic highway scan", "discover skill collections", "add new awesome-claude-skills sources".
allowed-tools: Read Write Edit Glob Grep Bash WebFetch WebSearch Agent
---

# Skill Directory Research Team

Discover, vet, and register Claude Skill directories that the **agentic highway scanner** ingests.

The skill orchestrates a team working in parallel:

- **Scouts** find candidate directories from GitHub, marketplaces, vendor pages, and recent posts.
- **Vetters** confirm each candidate is real, active, and spec-compliant.
- **The orchestrator** (you) dedupes against the existing registry, merges approved entries, and rebuilds the scan manifest.

Pass an optional lane to limit a run: `/skill-directory-research github` runs only the GitHub scout. With no arguments, run all four lanes.

## Inputs

| File | Purpose |
| --- | --- |
| `registry/skill-directories.yml` | Current registry. Source of truth for "already known". |
| `registry/scan-manifest.yml` | Derived scanner manifest. Regenerated after every run. |
| `${CLAUDE_SKILL_DIR}/references/seed-directories.md` | Annotated seed list. Brief scouts on what is already canonical. |
| `${CLAUDE_SKILL_DIR}/references/scout-brief.md` | Full scout prompt template. |
| `${CLAUDE_SKILL_DIR}/references/vet-brief.md` | Full vetter prompt template. |
| `${CLAUDE_SKILL_DIR}/references/registry-schema.md` | Schema docs for registry and manifest. |

## Workflow

### 1. Load known state

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/validate_registry.py registry/skill-directories.yml
```

The validator prints the entry count and exits non-zero on schema errors. If `registry/skill-directories.yml` is missing, bootstrap from the seed list:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/seed_registry.py \
  ${CLAUDE_SKILL_DIR}/references/seed-directories.md \
  > registry/skill-directories.yml
```

Record the entry count — the run report compares before/after.

### 2. Dispatch scouts in parallel

Spawn four scouts in a single Agent message — independent search lanes, no shared state. Each scout receives the brief in `references/scout-brief.md` plus its lane assignment. Use `subagent_type=Explore` so they have read-only web/search access without write tools.

| Lane | Where to look | Example queries |
| --- | --- | --- |
| `github-awesome` | GitHub repos named `awesome-claude-skills`, `claude-skills`, `agent-skills`, plus their forks and siblings | `awesome claude skills`, `agent skills directory` |
| `marketplaces` | Hosted marketplaces and registries that aggregate skills | `claude code plugin marketplace`, `agent skills registry` |
| `vendor-orgs` | First-party `skills/` directories under vendor or team GitHub orgs | `org:vercel skills`, `org:stripe skills`, `path:skills filename:SKILL.md` |
| `recent-posts` | Blog posts, talks, and changelogs from the last 90 days announcing new skills | `"new claude skill"`, `releasing skill`, `skill collection 2026` |

Tell each scout to return JSON only, matching the schema in `references/scout-brief.md`. Concatenate results into `candidates.json`.

### 3. Dedupe candidates

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/dedupe_candidates.py \
  registry/skill-directories.yml < candidates.json > new-candidates.json
```

Dedupe normalizes URLs (strips trailing slashes, lowercases hosts, rewrites `git@github.com:` to `https://github.com/...`) before comparing. New candidates are everything not already in the registry.

### 4. Dispatch vetters in parallel

For each new candidate, spawn a vetter (`subagent_type=general-purpose`) using the brief in `references/vet-brief.md`. Each vetter returns one verdict object: `{ candidate, decision: "approve" | "reject" | "defer", evidence, suggested_entry }`.

Cap a batch at ~12 vetters in one message. If there are more candidates, run additional batches sequentially. Collect verdicts into `verdicts.json`.

### 5. Merge approved entries

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/merge_candidates.py \
  registry/skill-directories.yml verdicts.json
```

Merge writes the updated registry sorted by `name`, sets `last_verified` to today on every approved entry, and is idempotent (rerunning with the same verdicts is a no-op).

### 6. Rebuild the scan manifest

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/build_scan_manifest.py \
  registry/skill-directories.yml > registry/scan-manifest.yml
```

The manifest is what the agentic highway scanner consumes. It strips registry metadata to a minimal `{ url, ref, kind, scan_paths }` shape per entry.

### 7. Report

Show a table with columns: **Name | URL | Decision | Evidence**. Group by approve / defer / reject. Cite the scout lane and the verifying check (HTTP code, SKILL.md path, last commit date) for every approved entry.

If the user has not asked you to commit, stop here and let them review. Only commit and push after explicit confirmation.

## Conventions for additions

- `name`: lowercase with hyphens; for GitHub use `owner-repo`; for marketplaces use the hostname stem.
- `url`: canonical https URL, no trailing slash.
- `kind`: one of `collection`, `individual`, `marketplace`, `awesome-list`, `vendor-skills`, `registry`.
- `ref`: branch or tag the scanner should pin to. Default `main`.
- `scan_paths`: globs relative to the directory root that point at `SKILL.md` files.
  - `collection`: typically `skills/*/SKILL.md`.
  - `individual`: typically `SKILL.md`.
  - `marketplace` / `awesome-list`: leave empty; mark `requires_crawler: true` so the scanner knows to follow links.
- `last_verified`: ISO date the vetter confirmed accessibility.
- `tags`: free-form lowercase tags (`official`, `vendor`, `community`, `1k+`, `vetted-2026`, etc.).

See `references/registry-schema.md` for the full schema.

## When to reject or defer

**Reject** when any of:

- Repository is archived, unreachable, or last update >18 months ago with no `SKILL.md`.
- No `SKILL.md` or compatible skill manifest discoverable.
- README or skill content advertises destructive payloads, credential exfiltration, or detection-evasion tooling.
- Duplicate of an entry already in the registry under a different URL (link the existing entry in the verdict).

**Defer** when promising but uncertain:

- Skills gated behind login or paid signup.
- Brand-new directory with no skills yet.
- Unverified author and no independent mirrors or stars.

Deferred entries are appended to `registry/deferred.yml` (created lazily) so the next run can re-evaluate without re-discovering them.

## Optional: commit and PR

When the user asks to commit:

```bash
git add registry/skill-directories.yml registry/scan-manifest.yml registry/deferred.yml
git commit -m "registry: add N skill directories from research run"
git push -u origin "$(git branch --show-current)"
```

Open a PR against `main` summarising additions in the body. Do NOT auto-merge.
