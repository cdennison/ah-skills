#!/usr/bin/env python3
"""Fetch Anthropic's official Claude plugin marketplace listing and feed the
repos it references into repo-seeds/registry.json as source="marketplace".

Source: https://github.com/anthropics/claude-plugins-official
    (.claude-plugin/marketplace.json)

Each plugin entry names a repo one of three ways:
  - {"source": "url", "url": "https://github.com/owner/repo.git", ...}
  - {"source": "git-subdir", "url": "https://github.com/owner/repo.git",
     "path": "plugins/x", ...}          -- we still clone the whole repo;
                                            extract_search_raw.py already
                                            finds SKILL.md anywhere under it
  - {"source": "github", "repo": "owner/repo", ...}
  - a bare string like "./plugins/x"    -- the plugin lives inside the
                                            marketplace repo itself
                                            (anthropics/claude-plugins-public
                                            in every case we've seen); we
                                            resolve via `homepage` when
                                            possible and fall back to that
                                            default repo otherwise

Note: we always shallow-clone the repo's default branch (--depth 1), same as
every other registry source -- we do NOT pin to the `ref`/`sha`/`commit` a
plugin manifest specifies. That metadata is kept on the registry entry for
reference, but the actual clone can drift from what a given plugin version
pins to.

Like registry.py's sync-seed, this is never destructive -- but it is NOT
"skip repos already in the registry." A repo already tracked via seed/
search/manual gets a "marketplace" source descriptor ADDED to its existing
entry if it doesn't have one yet, so overlap between discovery channels is
visible (see registry.py's module docstring) rather than hidden by whichever
source happened to add it first. Only a repo's existing descriptor of the
*same* type (marketplace re-run) gets updated in place; every other source
descriptor on that repo, and its skip status, are left untouched.

Usage:
    ./fetch_marketplace.py
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import registry

MARKETPLACE_URL = "https://raw.githubusercontent.com/anthropics/claude-plugins-official/refs/heads/main/.claude-plugin/marketplace.json"
CACHE_FILE = Path(__file__).parent / "repo-seeds" / "claude_plugins_marketplace.json"

# Fallback repo for plugins whose source is a bare local path (e.g.
# "./plugins/x") and whose homepage doesn't resolve to a github.com URL --
# every such case observed so far is a plugin bundled inside this repo.
INLINE_PLUGIN_FALLBACK_REPO = ("anthropics", "claude-plugins-public")

GITHUB_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)")


def fetch_marketplace() -> dict:
    req = urllib.request.Request(MARKETPLACE_URL, headers={"User-Agent": "ah-skills-search-demo"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[error] could not fetch marketplace.json: {e}", file=sys.stderr)
        sys.exit(1)
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_bytes(data)
    return json.loads(data)


def resolve_repo(plugin: dict) -> tuple[str, str] | None:
    source = plugin["source"]

    if isinstance(source, dict):
        if source.get("source") == "github" and "repo" in source:
            owner, _, repo = source["repo"].partition("/")
            return owner, repo
        if "url" in source:
            m = GITHUB_URL_RE.search(source["url"])
            if m:
                return m.group(1), m.group(2)
        return None

    # Bare string source: plugin lives inside some repo, identified via homepage.
    homepage = plugin.get("homepage", "")
    m = GITHUB_URL_RE.search(homepage)
    if m:
        return m.group(1), m.group(2)
    return INLINE_PLUGIN_FALLBACK_REPO


def sync_marketplace() -> tuple[list[dict], list[dict]]:
    """Returns (new_repos, newly_overlapping_repos): new_repos are repos not
    tracked under any source before; newly_overlapping_repos were already in
    the registry (via seed/search/manual) and just gained their first
    "marketplace" descriptor."""
    data = fetch_marketplace()
    plugins = data.get("plugins", [])

    registry_data = registry.load_registry()
    new_repos, new_overlaps = [], []
    unresolved = []
    seen_this_pass = set()

    for plugin in plugins:
        resolved = resolve_repo(plugin)
        if not resolved:
            unresolved.append(plugin["name"])
            continue
        owner, repo = resolved
        key = (owner.lower(), repo.lower())
        if key in seen_this_pass:
            continue  # two marketplace plugins pointing at the same repo -- one descriptor is enough
        seen_this_pass.add(key)

        existing = registry.find(registry_data, owner, repo)
        had_marketplace_source = existing is not None and "marketplace" in registry.source_types(existing)
        entry = registry.upsert(registry_data, {
            "owner": owner,
            "repo": repo,
            "source": "marketplace",
            "listing_url": MARKETPLACE_URL,
            "plugin": plugin["name"],
        })
        if existing is None:
            new_repos.append(entry)
        elif not had_marketplace_source:
            new_overlaps.append(entry)

    registry.save_registry(registry_data)
    if unresolved:
        print(f"[warn] could not resolve a repo for {len(unresolved)} plugin(s): {', '.join(unresolved)}", file=sys.stderr)
    return new_repos, new_overlaps


def main():
    new_repos, new_overlaps = sync_marketplace()
    for entry in new_repos:
        plugin = next(s for s in entry["sources"] if s["type"] == "marketplace")["plugin"]
        print(f"Added {entry['owner']}/{entry['repo']} (source=marketplace, plugin={plugin!r})")
    for entry in new_overlaps:
        plugin = next(s for s in entry["sources"] if s["type"] == "marketplace")["plugin"]
        types = "+".join(registry.source_types(entry))
        print(f"{entry['owner']}/{entry['repo']} already tracked -- also found in marketplace as {plugin!r} (now {types})")
    print(f"{len(new_repos)} new repo(s), {len(new_overlaps)} newly-overlapping repo(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
