#!/usr/bin/env python3
"""Shared skill_id derivation: given any file path under search-raw/, find
the enclosing skill directory (the one that actually contains SKILL.md) and
return it relative to search-raw/.

This is filesystem-verified rather than name-pattern-based, because some
mirror repos (e.g. NeverSight/learn-skills.dev) nest skills under directory
names that don't literally contain "skills" (e.g. "data/skills-md/..."),
which broke the old heuristic and collapsed thousands of distinct skills
into one bogus per-repo key.
"""

from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEARCH_RAW = HERE.parent / "search-raw"


@lru_cache(maxsize=None)
def skill_id_from_path(path_str: str) -> str:
    p = Path(path_str)
    if not p.is_absolute():
        p = SEARCH_RAW / p

    current = p if p.is_dir() else p.parent
    while True:
        try:
            rel = current.relative_to(SEARCH_RAW)
        except ValueError:
            break
        if (current / "SKILL.md").exists():
            return str(rel)
        if current == SEARCH_RAW:
            break
        current = current.parent

    # No SKILL.md found anywhere up the chain (shouldn't normally happen for
    # files under search-raw/) -- fall back to the file's own parent dir.
    try:
        rel = p.parent.relative_to(SEARCH_RAW)
    except ValueError:
        rel = p.parent
    return str(rel)
