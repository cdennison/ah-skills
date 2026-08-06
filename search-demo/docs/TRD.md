# Technical Requirements Document: Agent Skills Search

## Scope note

This TRD covers the core search/indexing problem only. Auth, deployment topology, scaling, multi-tenancy, and other production-readiness concerns are explicitly out of scope for now — we're validating the core idea, not shipping a hardened service.

## Problem

Agents (and the humans directing them) need to find the right "skill" (a packaged capability — script, prompt, tool definition) for a task. Skills are scattered across multiple registries and repos on the internet, of wildly varying quality, and none of the existing marketplaces let you search the way agents actually need to search.

Concretely, we need to handle queries like:

> "turn a spreadsheet into a chart"

and return a relevant skill even if the skill's name, description, and README never contain those exact words — the system has to understand *intent*, not just match tokens.

## Why existing marketplaces fall short

Surveyed marketplaces:

- https://officialskills.sh/
- https://www.skills.sh/
- https://openskills.cc/

These are largely keyword/metadata search over a list — no semantic understanding of what a skill *does*. A query like "turn a spreadsheet into a chart" fails unless the skill happens to contain those words.

The one exception we've found: **ClawHub**, which does support semantic search built for agent use cases. That's the bar — everyone else is behind it.

## Core requirements

### 1. Multi-source indexing

Index skills from multiple reliable sources across the internet (GitHub repos, registries/marketplaces like the ones above, and others as discovered) rather than relying on a single source of truth. No one registry is complete or trustworthy enough on its own.

### 2. Popularity / signal tracking

Track *where* a given skill is indexed (which registries, how many repos reference or bundle it, stars/forks if available) as a proxy signal for popularity and, hopefully, a weak proxy for functionality/quality. A skill indexed in three independent registries is more likely to be legitimate and useful than one found in a single obscure repo.

### 3. Hybrid semantic search

This is the central technical bet. Search must combine:

- **Semantic/vector search** — embed skill descriptions/content so intent-based queries ("turn a spreadsheet into a chart") match skills like "csv-to-chart" or "excel-plotter" even without keyword overlap.
- **Keyword/lexical search** — exact matches still matter (tool names, specific library references, exact skill names).

Neither alone is sufficient; the hybrid combination is the differentiator versus the existing marketplaces listed above.

### 4. Multi-criteria reasoning for agents

An agent picking a skill isn't just doing top-1 retrieval — it needs to reason about tradeoffs across several criteria simultaneously: relevance to the task, popularity/trust signal, source reliability, recency/maintenance status, and functional fit (does it actually do the narrow thing requested, or something adjacent). The search layer should surface enough structured metadata per result (not just a raw ranked list) that an agent can weigh these criteria itself rather than blindly taking result #1.

### 5. Aggressive curation / blacklisting

Given the volume of low-quality, broken, abandoned, or malicious skills across public sources, the index needs active curation:

- A blacklist mechanism to exclude skills that shouldn't be surfaced at all (broken, unsafe, spam, duplicates, abandoned).
- Bias toward being aggressive about exclusion — a smaller trustworthy index beats a large noisy one for this use case.

### 6. Freshness via daily cron

Skills, their metadata, and their popularity signals change. The index needs to be kept up to date via a daily scheduled job that re-crawls sources, re-scores popularity, and re-applies blacklist rules (in case previously-blacklisted skills are fixed, or previously-fine skills go stale/get flagged).

## Out of scope (for now)

- Authentication / authorization
- Production deployment, scaling, HA
- Rate limiting, abuse prevention
- Billing/usage metering
- UI polish

These will matter eventually but aren't blockers for validating the core indexing + hybrid search + curation loop.
