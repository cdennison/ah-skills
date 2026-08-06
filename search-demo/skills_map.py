#!/usr/bin/env python3
"""repo-seeds/skills.json -- maps each skill NAME to every repo it was found
in, so the same skill turning up in multiple repos (mirrors, forks, a skill
vendored into more than one collection) is visible instead of silently
producing look-alike, disconnected search results.

This is the counterpart to registry.json (which maps one repo to the N
registry sources that surfaced it): here the key is the skill, and the
value is the N repos it lives in, each carrying that repo's own registry
sources for context.

Skill identity is the frontmatter `name:` (falling back to the parent
directory name, same rule `index_qdrant.py` uses for its payload `name`
field) -- resolved by reading SKILL.md files straight off disk under
repos/, not from source-specific metadata like a marketplace "plugin"
name, so it works the same way regardless of which registry channel
(seed/search/manual/marketplace) found the repo.

update_repo(owner, repo) is called from clone_repos.py's main loop for
*every* repo it processes each run, whether that repo was actually
git-cloned or skipped because it already exists on disk -- registry
sources can gain a new descriptor between runs even when a repo's content
doesn't change, so this file needs refreshing every run regardless of
clone outcome. It replaces this repo's entries wholesale under every
skill name (dropping ones that no longer resolve, e.g. a renamed or
removed SKILL.md) and leaves every other repo's entries untouched.
"""

import json
from pathlib import Path

import registry
from frontmatter import parse_frontmatter

REPOS_DIR = Path(__file__).parent / "repos"
SKILLS_FILE = Path(__file__).parent / "repo-seeds" / "skills.json"


def load_skills_map() -> list[dict]:
    if not SKILLS_FILE.exists():
        return []
    return json.loads(SKILLS_FILE.read_text())


def save_skills_map(skills_map: list[dict]) -> None:
    skills_map = sorted(skills_map, key=lambda s: s["name"].lower())
    for entry in skills_map:
        entry["repos"] = sorted(entry["repos"], key=lambda r: (r["owner"].lower(), r["repo"].lower()))
    SKILLS_FILE.write_text(json.dumps(skills_map, indent=2) + "\n")


def find_repo_skills(owner: str, repo: str) -> list[dict]:
    """(name, path, url) for every SKILL.md under repos/<owner>/<repo>,
    read straight off disk -- works regardless of which registry channel
    found the repo."""
    repo_dir = REPOS_DIR / owner / repo
    if not repo_dir.is_dir():
        return []
    found = []
    for p in sorted(repo_dir.rglob("*.md")):
        if ".git" in p.parts or p.name.lower() != "skill.md":
            continue
        rel = p.relative_to(REPOS_DIR)
        subpath = "/".join(rel.parts[2:])
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            # e.g. a broken symlink committed upstream (target doesn't
            # exist in this repo) -- skip it rather than take down the
            # whole clone_repos.py run over one bad repo.
            print(f"[warn] skipping unreadable {rel}: {e}")
            continue
        meta = parse_frontmatter(text)
        found.append({
            "name": meta.get("name", p.parent.name),
            "path": str(rel),
            "url": f"https://github.com/{owner}/{repo}/blob/HEAD/{subpath}",
        })
    return found


def update_repo(owner: str, repo: str, skills_map: list[dict] | None = None, save: bool = True) -> list[dict]:
    """Refresh this repo's entries across the skill map from what's on disk
    right now plus its current registry.json sources."""
    skills_map = load_skills_map() if skills_map is None else skills_map

    registry_entry = registry.find(registry.load_registry(), owner, repo)
    sources = registry_entry["sources"] if registry_entry else []

    by_name = {s["name"]: s for s in skills_map}

    # Drop this repo's old entries from every skill (it may have renamed or
    # removed a SKILL.md since the last run), then re-add current ones.
    for entry in by_name.values():
        entry["repos"] = [r for r in entry["repos"] if not (r["owner"] == owner and r["repo"] == repo)]

    for skill in find_repo_skills(owner, repo):
        entry = by_name.setdefault(skill["name"], {"name": skill["name"], "repos": []})
        entry["repos"].append({
            "owner": owner,
            "repo": repo,
            "path": skill["path"],
            "url": skill["url"],
            "sources": sources,
        })

    skills_map[:] = [e for e in by_name.values() if e["repos"]]
    if save:
        save_skills_map(skills_map)
    return skills_map


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        print("usage: ./skills_map.py owner/repo", file=sys.stderr)
        sys.exit(1)
    owner, _, repo = sys.argv[1].partition("/")
    skills_map = update_repo(owner, repo)
    mine = [e for e in skills_map for r in e["repos"] if r["owner"] == owner and r["repo"] == repo]
    print(f"{len(mine)} skill(s) mapped for {owner}/{repo}")
