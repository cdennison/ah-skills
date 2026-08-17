#!/usr/bin/env python3
"""mcp-repo-seeds/registry.json -- the MCP pipeline's tracking file, same
role as ../registry.py plays for the skills pipeline (see
PROPOSED_PIPELINE.md's side-by-side table for why this is a *separate*
file/schema rather than a shared one -- the two pipelines share a pattern,
not state).

**One row per unique server, `sources` is a list, overlap is expected.**
Same idea as ../registry.py: the official registry, Glama, and the
awesome-mcp-servers seed list routinely surface the same server (the seed
list's own README says it's synced with Glama) -- that's corroborating
signal, not a duplicate to collapse away, so every row keeps a `sources`
list of every channel that ever surfaced it instead of overwriting.

**Identity key**: prefer the server's GitHub repo (`github:<owner>/<repo>`,
lowercased) when one can be resolved -- this is what lets all three sources
dedupe onto one row for the same server. Falls back to a source-scoped id
(`official:<name>`, `glama:<slug>`) for entries with no resolvable GitHub
link (e.g. a closed-source remote-only server) -- these can't be deduped
across sources without a repo to key on, which is a real, documented
limitation, not a bug.

Every entry also carries `errors`: a list of {date, source, message}, never
overwritten -- a failed fetch/scan is itself useful information (this repo
is gone, this manifest 404s), so it's recorded rather than silently
dropped. `status` is "active" (default) or "error" (every fetch attempt for
this entry has failed so far -- still kept, not removed, since the source
list still names it).

Use this module's functions -- don't hand-edit registry.json directly.
"""

import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GITHUB_REPO_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")

MCP_DIR = Path(__file__).parent
REPO_SEEDS_DIR = MCP_DIR.parent / "mcp-repo-seeds"
REGISTRY_FILE = REPO_SEEDS_DIR / "registry.json"
SEED_LISTS_FILE = REPO_SEEDS_DIR / "repo_seeds.json"
RAW_DIR = MCP_DIR.parent / "mcp-search-raw"
README_DIR = RAW_DIR / "readmes"

VALID_SOURCES = {"official_registry", "glama", "awesome-mcp-servers"}


def parse_github_repo_url(url: str) -> tuple[str, str] | None:
    match = GITHUB_REPO_RE.search(url or "")
    if not match:
        return None
    return match.group(1), match.group(2).rstrip(".")


def make_id(repo_url: str | None, source_type: str, source_key: str) -> str:
    """Identity key for a registry row -- see module docstring."""
    owner_repo = parse_github_repo_url(repo_url) if repo_url else None
    if owner_repo:
        owner, repo = owner_repo
        return f"github:{owner.lower()}/{repo.lower()}"
    return f"{source_type}:{source_key}"


def readme_path_for(repo_url: str) -> Path:
    owner, repo = parse_github_repo_url(repo_url)
    return README_DIR / f"{owner}__{repo}.md"


def load_registry() -> list[dict]:
    if not REGISTRY_FILE.exists():
        return []
    return json.loads(REGISTRY_FILE.read_text())


def save_registry(registry: list[dict]) -> None:
    REPO_SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    registry = sorted(registry, key=lambda r: r["id"])
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2) + "\n")


def find(registry: list[dict], entry_id: str, index: dict[str, dict] | None = None) -> dict | None:
    """`index`, if given (see build_index()), makes this O(1) instead of an
    O(n) linear scan -- use it for any loop calling find()/upsert() more
    than a handful of times (bulk pulls at 10k-100k scale turn the default
    linear scan into O(n^2) otherwise, which is exactly what made the first
    100k-scale glama/official-registry pull crawl)."""
    if index is not None:
        return index.get(entry_id)
    for r in registry:
        if r["id"] == entry_id:
            return r
    return None


def build_index(registry: list[dict]) -> dict[str, dict]:
    """id -> row lookup. Build once per script run before a bulk upsert
    loop and pass it to every find()/upsert() call in that loop (see
    pull_official_registry.py, pull_glama.py, pull_seed_repo.py)."""
    return {r["id"]: r for r in registry}


def source_types(entry: dict) -> set[str]:
    return {s["type"] for s in entry.get("sources", [])}


def get_source(entry: dict, source_type: str) -> dict | None:
    return next((s for s in entry.get("sources", []) if s["type"] == source_type), None)


def upsert(registry: list[dict], entry: dict, index: dict[str, dict] | None = None) -> dict:
    """Insert or update a row, ADDING a source descriptor rather than
    overwriting provenance -- same contract as ../registry.py's upsert().

    `entry` shape: {"repo_url", "source", "name", "description"?, ...
    source-type-specific detail fields}. `id` is derived via make_id() using
    `entry.get("source_key")` (required when repo_url can't resolve a
    GitHub owner/repo -- e.g. the official-registry server `name`, or a
    Glama `slug`) as the fallback identity.

    `name`/`description`/`repo_url` on the row itself are filled in on
    first insert and then only overwritten when the existing value is empty
    -- later sources corroborate, they don't clobber an earlier source's
    (possibly better) description. Glama's data is the one documented
    exception: PROPOSED_PIPELINE.md's source notes say to prefer Glama's
    description/attributes over self-derived versions, so a "glama" source
    is allowed to overwrite `description`.

    Pass `index` (see build_index()) when calling this in a loop -- without
    it, the existing-row lookup is an O(n) scan of `registry`, so a bulk
    pull of thousands of entries degrades to O(n^2).
    """
    if entry["source"] not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {entry['source']!r}")

    entry = dict(entry)
    source_type = entry.pop("source")
    repo_url = entry.pop("repo_url", None)
    source_key = entry.pop("source_key", None)
    name = entry.pop("name", None)
    description = entry.pop("description", None)

    entry_id = make_id(repo_url, source_type, source_key)
    # whatever's left is source-type-specific detail
    descriptor = {"type": source_type, "added": datetime.date.today().isoformat(), **entry}

    existing = find(registry, entry_id, index=index)
    if existing:
        existing_descriptor = next((s for s in existing["sources"] if s["type"] == source_type), None)
        if existing_descriptor:
            existing_descriptor.update({k: v for k, v in descriptor.items() if k != "added"})
        else:
            existing["sources"].append(descriptor)
        if repo_url and not existing.get("repo_url"):
            existing["repo_url"] = repo_url
        if name and not existing.get("name"):
            existing["name"] = name
        if description and (not existing.get("description") or source_type == "glama"):
            existing["description"] = description
        existing["status"] = "active"
        return existing

    row = {
        "id": entry_id,
        "name": name,
        "description": description,
        "repo_url": repo_url,
        "added": datetime.date.today().isoformat(),
        "status": "active",
        "sources": [descriptor],
        "errors": [],
    }
    registry.append(row)
    if index is not None:
        index[entry_id] = row
    return row


def record_error(
    registry: list[dict], entry_id: str, source: str, message: str, index: dict[str, dict] | None = None
) -> dict | None:
    """Append an error without removing the entry. If the entry has never
    successfully carried any data (no repo_url/name yet, no readme), mark it
    status="error" -- still kept for visibility, not deleted. Pass `index`
    (build_index()) in a loop over many entries -- same O(n^2) concern as
    upsert()."""
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry["errors"].append({
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "message": message,
    })
    return entry


def mark_readme(
    registry: list[dict], entry_id: str, path: Path, source: str, index: dict[str, dict] | None = None
) -> dict | None:
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry["readme_path"] = str(path.relative_to(MCP_DIR.parent))
    entry["readme_source"] = source
    entry["readme_fetched"] = datetime.datetime.now().isoformat(timespec="seconds")
    return entry


def set_stars(
    registry: list[dict], entry_id: str, stars: int, index: dict[str, dict] | None = None
) -> dict | None:
    """Record a GitHub stargazer count fetched by fetch_mcp_rankings.py, plus
    when it was fetched -- same shape as ../registry.py's set_stars() for the
    skills pipeline. No-ops (returns None) if entry_id isn't in the
    registry."""
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry["stars"] = stars
    entry["stars_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    return entry


def set_npm_ranking(
    registry: list[dict], entry_id: str, ranking: dict, index: dict[str, dict] | None = None
) -> dict | None:
    """Record npm ranking signal fetched by fetch_mcp_rankings.py. `ranking`
    keys land directly on the row (weekly_downloads, monthly_downloads,
    npm_dependents, npm_score_final/quality/popularity/maintenance -- see
    fetch_mcp_rankings.py's fetch_npm_search/fetch_npm_point for which keys
    are actually present for a given row). Only keys npm's API actually
    reported are included by the caller -- a field's absence means "this
    source didn't report it," not "it's zero," so this never writes a field
    as null just to keep the schema uniform.

    `downloads_source` is fixed at "npm" for now (the only download source
    this pipeline fetches) but kept as an explicit field rather than assumed,
    since a server could in principle draw this from more than one registry
    later (pypi, say)."""
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry.update(ranking)
    entry["downloads_source"] = "npm"
    entry["downloads_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    return entry


# --- seed list tracking (mcp-repo-seeds/repo_seeds.json) -- mirrors
# ../registry.py's repo_seeds.json exactly, just pointed at a separate file. ---

def load_seed_lists() -> list[dict]:
    if not SEED_LISTS_FILE.exists():
        return []
    return json.loads(SEED_LISTS_FILE.read_text())


def save_seed_lists(seed_lists: list[dict]) -> None:
    REPO_SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    SEED_LISTS_FILE.write_text(json.dumps(seed_lists, indent=2) + "\n")


def mark_seed_pulled(name: str, upstream_repo: str, vendored_path: str) -> dict:
    seed_lists = load_seed_lists()
    entry = next((s for s in seed_lists if s["name"] == name), None)
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if entry is None:
        entry = {"name": name, "upstream_repo": upstream_repo, "vendored_path": vendored_path, "last_pulled": now}
        seed_lists.append(entry)
    else:
        entry["last_pulled"] = now
    save_seed_lists(seed_lists)
    return entry
