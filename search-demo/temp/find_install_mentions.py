#!/usr/bin/env python3
"""Search all skill files under search-raw for install-related mentions
(npm install, pip install, or general dependency installation)."""

import re
from pathlib import Path

SEARCH_DIR = Path(__file__).resolve().parent.parent / "search-raw"
LOG_FILE = Path(__file__).resolve().parent / "install_mentions.log"

PATTERNS = [
    r"npm install",
    r"npm i\s",
    r"pip install",
    r"pip3 install",
    r"pipx install",
    r"yarn add",
    r"pnpm add",
    r"pnpm install",
    r"cargo install",
    r"brew install",
    r"apt(-get)? install",
    r"go install",
    r"gem install",
    r"conda install",
    r"uv pip install",
    r"uv add",
    r"install dependencies",
    r"install the dependencies",
    r"requires? (you )?to install",
]

COMBINED = re.compile("|".join(PATTERNS), re.IGNORECASE)

TEXT_EXTS = {".md", ".py", ".sh", ".txt", ".yaml", ".yml", ".json", ".js", ".ts"}


def main():
    matches = []
    files_scanned = 0

    for path in SEARCH_DIR.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_EXTS:
            continue
        files_scanned += 1
        try:
            text = path.read_text(errors="ignore")
        except Exception as e:
            print(f"Could not read {path}: {e}")
            continue

        for i, line in enumerate(text.splitlines(), start=1):
            if COMBINED.search(line):
                matches.append((str(path), i, line.strip()))

    print(f"Scanned {files_scanned} files.")
    print(f"Found {len(matches)} matching lines.")

    with LOG_FILE.open("w") as f:
        f.write(f"Scanned {files_scanned} files under {SEARCH_DIR}\n")
        f.write(f"Found {len(matches)} matching lines.\n\n")
        for path, lineno, line in matches:
            entry = f"{path}:{lineno}: {line}"
            f.write(entry + "\n")

    print(f"Details written to {LOG_FILE}")


if __name__ == "__main__":
    main()
