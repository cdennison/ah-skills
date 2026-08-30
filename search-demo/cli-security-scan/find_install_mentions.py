#!/usr/bin/env python3
"""Sweep every text file under search-raw/ for install-command mentions
(npm/pip/pipx/yarn/pnpm/uv/cargo/brew/... install) and log each hit.

Output: work/install_mentions.log, one line per hit:

    <path-relative-to-search-raw>:<lineno>: <the matching line, stripped>

Paths are stored **relative to search-raw/** so the downstream skill-id join
(map_to_skills.py, build_cli_export.py via skill_id_util) works on any
machine -- an earlier prototype logged absolute paths and the join silently
matched nothing off the author's laptop.
"""

import re

from _common import SEARCH_RAW, WORK

LOG_FILE = WORK / "install_mentions.log"

PATTERNS = [
    r"npm install",
    r"npm i\s",
    r"npm exec",
    r"pip install",
    r"pip3 install",
    r"pipx install",
    r"pipx run",
    r"yarn add",
    r"pnpm add",
    r"pnpm install",
    r"pnpm dlx",
    r"npx ",
    r"uvx ",
    r"cargo install",
    r"brew install",
    r"apt(-get)? install",
    r"go install",
    r"gem install",
    r"conda install",
    r"uv pip install",
    r"uv add",
    r"uv tool install",
    r"install dependencies",
    r"install the dependencies",
    r"requires? (you )?to install",
]

COMBINED = re.compile("|".join(PATTERNS), re.IGNORECASE)

TEXT_EXTS = {".md", ".py", ".sh", ".txt", ".yaml", ".yml", ".json", ".js", ".ts", ".mjs", ".cjs"}


def main():
    matches = []
    files_scanned = 0

    for path in SEARCH_RAW.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        files_scanned += 1
        try:
            text = path.read_text(errors="ignore")
        except OSError as e:
            print(f"Could not read {path}: {e}")
            continue

        rel = path.relative_to(SEARCH_RAW)
        for i, line in enumerate(text.splitlines(), start=1):
            if COMBINED.search(line):
                matches.append((str(rel), i, line.strip()))

    print(f"Scanned {files_scanned} files.")
    print(f"Found {len(matches)} matching lines.")

    with LOG_FILE.open("w") as f:
        f.write(f"Scanned {files_scanned} files under {SEARCH_RAW}\n")
        f.write(f"Found {len(matches)} matching lines.\n\n")
        for rel, lineno, line in matches:
            f.write(f"{rel}:{lineno}: {line}\n")

    print(f"Details written to {LOG_FILE}")


if __name__ == "__main__":
    main()
