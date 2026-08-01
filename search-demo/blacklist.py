#!/usr/bin/env python3
"""repo-seeds/skill_blacklist.json -- individual SKILL.md files to exclude
from the pipeline, keyed by their path relative to repos/ (e.g.
"owner/repo/skills/some-skill/SKILL.md"), each with a required reason.

Unlike registry.json's status=skip (which is currently inert), this
blacklist IS enforced: extract_search_raw.py skips any file whose relative
path is listed here, so it never lands in search-raw/, and index_qdrant.py's
existing hash-diff logic then removes it from Qdrant on the next index run
(it looks like the file disappeared, which -- from search-raw/'s perspective
-- it did).

Caveat: blacklisting a skill that's already been indexed only takes effect
after you rerun extract_search_raw.py + index_qdrant.py -- it's not a live
filter on top of the existing qdrant_db/.

CLI usage:
    ./blacklist.py add owner/repo/path/to/SKILL.md "reason this is excluded"
    ./blacklist.py remove owner/repo/path/to/SKILL.md
    ./blacklist.py list
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

BLACKLIST_FILE = Path(__file__).parent / "repo-seeds" / "skill_blacklist.json"


def load_blacklist() -> list[dict]:
    if not BLACKLIST_FILE.exists():
        return []
    return json.loads(BLACKLIST_FILE.read_text())


def save_blacklist(blacklist: list[dict]) -> None:
    blacklist = sorted(blacklist, key=lambda b: b["path"].lower())
    BLACKLIST_FILE.write_text(json.dumps(blacklist, indent=2) + "\n")


def blacklisted_paths(blacklist: list[dict] | None = None) -> set[str]:
    """Set of relative-path strings (as extract_search_raw.py would compute
    them: str(path.relative_to(REPOS_DIR))) to exclude."""
    blacklist = blacklist if blacklist is not None else load_blacklist()
    return {b["path"] for b in blacklist}


def add(path: str, reason: str) -> dict:
    if not reason or not reason.strip():
        raise ValueError("blacklisting a skill requires a non-empty reason")
    blacklist = load_blacklist()
    existing = next((b for b in blacklist if b["path"] == path), None)
    if existing:
        existing["reason"] = reason.strip()
        existing["added"] = datetime.date.today().isoformat()
        entry = existing
    else:
        entry = {"path": path, "reason": reason.strip(), "added": datetime.date.today().isoformat()}
        blacklist.append(entry)
    save_blacklist(blacklist)
    return entry


def remove(path: str) -> bool:
    blacklist = load_blacklist()
    match = next((b for b in blacklist if b["path"] == path), None)
    if not match:
        return False
    blacklist.remove(match)
    save_blacklist(blacklist)
    return True


def _cli():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Blacklist one skill file with a required reason")
    p_add.add_argument("path", help="Path relative to repos/, e.g. owner/repo/skills/name/SKILL.md")
    p_add.add_argument("reason", help="Why this skill is excluded")

    p_remove = sub.add_parser("remove", help="Un-blacklist a skill file")
    p_remove.add_argument("path", help="Path relative to repos/")

    sub.add_parser("list", help="List blacklisted skills")

    args = parser.parse_args()

    if args.cmd == "add":
        entry = add(args.path, args.reason)
        print(f"Blacklisted {entry['path']}")

    elif args.cmd == "remove":
        if remove(args.path):
            print(f"Removed {args.path} from blacklist")
        else:
            print(f"[error] {args.path} not found in blacklist", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "list":
        blacklist = load_blacklist()
        for b in blacklist:
            print(f"{b['path']}  -- {b['reason']}")
        print(f"\n{len(blacklist)} blacklisted", file=sys.stderr)


if __name__ == "__main__":
    _cli()
