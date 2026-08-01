#!/usr/bin/env python3
"""Extract SKILL.md files (per the agentskills.io spec) from cloned repos into /search-raw.

We only care about actual skill definitions (SKILL.md), not general repo READMEs.
Some repos document their skills collection in a top-level README that sits one
level above the skills/ directory rather than inside each skill folder — those
are called out explicitly in EXTRA_README_REPOS below.
"""

import shutil
from pathlib import Path

REPOS_DIR = Path(__file__).parent / "repos"
DEST_DIR = Path(__file__).parent / "search-raw"

# Match SKILL.md (any casing), skip anything under .git
TARGET_NAMES = {"skill.md"}

# owner/repo pairs whose top-level README (one level up from skills/) should
# also be indexed, since it documents the skills collection itself.
EXTRA_README_REPOS = {
    ("google-gemini", "gemini-skills"),
}


def find_target_files():
    """Yield (path, is_skill) pairs. is_skill=False marks the extra top-level
    READMEs, which are indexed for search but don't represent a single skill."""
    for path in REPOS_DIR.rglob("*.md"):
        if ".git" in path.parts:
            continue
        if path.name.lower() in TARGET_NAMES:
            yield path, True

    for owner, repo in EXTRA_README_REPOS:
        readme = REPOS_DIR / owner / repo / "README.md"
        if readme.exists():
            yield readme, False


def main():
    DEST_DIR.mkdir(exist_ok=True)

    skill_count = 0
    readme_count = 0
    repos = set()
    total_chars = 0
    total_lines = 0
    total_bytes = 0

    for src, is_skill in find_target_files():
        rel = src.relative_to(REPOS_DIR)
        dest = DEST_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

        text = src.read_text(encoding="utf-8", errors="replace")
        if is_skill:
            skill_count += 1
        else:
            readme_count += 1
        repos.add(rel.parts[0] + "/" + rel.parts[1])
        total_chars += len(text)
        total_lines += text.count("\n") + 1
        total_bytes += src.stat().st_size

    total_files = skill_count + readme_count
    avg_chars = total_chars / total_files if total_files else 0

    print("--- extract_search_raw stats ---")
    print(f"Skills:          {skill_count:,}")
    print(f"Extra READMEs:   {readme_count:,}")
    print(f"Total files:     {total_files:,}")
    print(f"Repos covered:   {len(repos):,}")
    print(f"Total lines:     {total_lines:,}")
    print(f"Total chars:     {total_chars:,}")
    print(f"Total size:      {total_bytes:,} bytes ({total_bytes / 1024 / 1024:.2f} MB)")
    print(f"Avg chars/file:  {avg_chars:,.0f}")
    print(f"Output dir:      {DEST_DIR}")
    print("---------------------------------")


if __name__ == "__main__":
    main()
