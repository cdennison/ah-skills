#!/usr/bin/env python3
"""Extract SKILL.md files (per the agentskills.io spec) from cloned repos into /search-raw.

For every SKILL.md we also look for a README.md that documents it, either
sitting in the same directory or one directory up (e.g. a skills collection's
top-level README). When found, it's extracted alongside the SKILL.md's
destination directory -- flattened to that dir even if it originally lived a
level up -- so each skill folder in /search-raw carries both files together.
"""

import shutil
from pathlib import Path

from blacklist import blacklisted_paths

REPOS_DIR = Path(__file__).parent / "repos"
DEST_DIR = Path(__file__).parent / "search-raw"

# Match SKILL.md (any casing), skip anything under .git
TARGET_NAMES = {"skill.md"}


def find_readme(skill_path):
    """Look for a README.md next to skill_path, then one directory up."""
    for candidate_dir in (skill_path.parent, skill_path.parent.parent):
        for readme_name in ("README.md", "readme.md", "Readme.md"):
            readme = candidate_dir / readme_name
            if readme.exists():
                return readme
    return None


def find_target_files():
    """Yield (src, dest_rel, is_skill) triples. dest_rel is the path relative
    to REPOS_DIR that the file should be extracted under -- for READMEs found
    one directory up from their SKILL.md, this flattens them into the skill's
    own directory so both files land together in /search-raw."""
    for path in REPOS_DIR.rglob("*.md"):
        if ".git" in path.parts:
            continue
        if path.name.lower() not in TARGET_NAMES:
            continue

        rel = path.relative_to(REPOS_DIR)
        yield path, rel, True

        readme = find_readme(path)
        if readme is not None:
            readme_rel = rel.parent / readme.name
            yield readme, readme_rel, False


def main():
    DEST_DIR.mkdir(exist_ok=True)

    blacklisted = blacklisted_paths()
    skill_count = 0
    readme_count = 0
    blacklisted_count = 0
    broken_count = 0
    repos = set()
    total_chars = 0
    total_lines = 0
    total_bytes = 0

    for src, rel, is_skill in find_target_files():
        if str(rel) in blacklisted:
            blacklisted_count += 1
            stale_dest = DEST_DIR / rel
            if stale_dest.exists():
                # Was extracted before being blacklisted -- remove so index_qdrant.py's
                # hash-diff sees it as gone and drops it from Qdrant on the next run.
                stale_dest.unlink()
            continue

        if not src.exists():
            # Broken symlink upstream (target missing in the source repo itself).
            broken_count += 1
            print(f"[warn] broken symlink, skipping: {rel}")
            continue

        if src.is_dir():
            # Some repos have a SKILL.md/README.md *directory* rather than a
            # file (e.g. a docs route folder) -- not a real skill file.
            print(f"[warn] {rel} is a directory, not a file, skipping")
            continue

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
    print(f"Blacklisted:     {blacklisted_count:,}")
    print(f"Broken symlinks: {broken_count:,}")
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
