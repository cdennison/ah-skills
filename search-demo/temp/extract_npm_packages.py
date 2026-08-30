#!/usr/bin/env python3
"""Extract npm package names mentioned in install commands across all skills,
then classify which ones are CLI tools (vs plain libraries) by querying the
npm registry for a `bin` field.

Two phases:
  1. extract  - parse install_mentions.log, pull out npm install/i/npx/yarn
               add/pnpm add commands, extract package names, count occurrences,
               write npm_packages.csv (sorted by frequency).
  2. classify - for each unique package, hit the npm registry API
               (registry.npmjs.org/<pkg>) and check for a "bin" field (CLI),
               "types"/"main" only (library), keywords containing "cli", etc.
               Writes npm_packages_classified.csv.

Usage:
  python3 extract_npm_packages.py extract
  python3 extract_npm_packages.py classify [--limit N]
"""

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "install_mentions.log"
PACKAGES_CSV = HERE / "npm_packages.csv"
CLASSIFIED_CSV = HERE / "npm_packages_classified.csv"

# Matches: npm install|i, npx, yarn add, pnpm add|install  followed by
# one or more package specs (possibly scoped, possibly with -g/--save etc,
# possibly with @version).
CMD_RE = re.compile(
    r"\b(?:npm\s+(?:install|i)|npx|yarn\s+add|pnpm\s+(?:add|install))\b(?P<rest>[^\n`]*)",
    re.IGNORECASE,
)

# Only trust the command when it's inside backticks (inline code) or fenced
# code, or when it's the first token on its own line (shell-script style).
# This filters out prose like "treat every skill like an npm install".
BACKTICK_RE = re.compile(r"`([^`]*?)`")
LEADING_CMD_RE = re.compile(
    r"^\s*\$?\s*(npm\s+(?:install|i)|npx|yarn\s+add|pnpm\s+(?:add|install))\b",
    re.IGNORECASE,
)

FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][\w-]*$")

# A valid npm package spec: optional @scope/name, optional @version
PKG_RE = re.compile(r"^(@[a-z0-9][\w.-]*/[a-z0-9][\w.-]*|[a-z0-9][\w.-]*)(@[\w.^~><=|*-]+)?$")

STOPWORDS = {
    "-g", "--global", "-d", "--save-dev", "-s", "--save", "--production",
    "-y", "--yes", "-w", "--workspace",
    "and", "the", "a", "an", "then", "install", "installed", "installing",
    "package", "packages", "before", "run", "runs", "running", "npm",
    "npx", "yarn", "pnpm", "add", "i",
}


def _trusted_command_snippets(line: str):
    """Return substrings of `line` that are safe to treat as real shell
    commands: backtick-enclosed spans, or the whole line if it starts with
    the install command (ignoring leading whitespace/$)."""
    snippets = []
    for m in BACKTICK_RE.finditer(line):
        snippets.append(m.group(1))
    if LEADING_CMD_RE.match(line):
        snippets.append(line)
    return snippets


def extract_packages_from_line(line: str):
    pkgs = []
    for snippet in _trusted_command_snippets(line):
        for m in CMD_RE.finditer(snippet):
            rest = m.group("rest").strip()
            # stop at shell operators / comments / prose punctuation
            rest = re.split(r"[|&;#]|&&|\$\(|[.,](?:\s|$)", rest)[0]
            tokens = rest.split()
            for tok in tokens:
                tok = tok.strip("'\",()")
                if not tok:
                    continue
                if tok.lower() in STOPWORDS or FLAG_RE.match(tok):
                    continue
                if tok.startswith("-"):
                    continue
                mm = PKG_RE.match(tok)
                if not mm:
                    continue
                pkg_name = mm.group(1)
                pkgs.append(pkg_name)
    return pkgs


def extract():
    if not LOG_FILE.exists():
        print(f"Missing {LOG_FILE}, run find_install_mentions.py first.")
        sys.exit(1)

    counter = Counter()
    example_lines = {}

    with LOG_FILE.open(errors="ignore") as f:
        for raw in f:
            if ":" not in raw:
                continue
            line = raw.rstrip("\n")
            for pkg in extract_packages_from_line(line):
                counter[pkg] += 1
                example_lines.setdefault(pkg, line.split(":", 2)[-1].strip()[:120])

    with PACKAGES_CSV.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["package", "mentions", "example"])
        for pkg, count in counter.most_common():
            writer.writerow([pkg, count, example_lines.get(pkg, "")])

    print(f"Extracted {len(counter)} unique npm packages from {sum(counter.values())} mentions.")
    print(f"Written to {PACKAGES_CSV}")


def fetch_registry_info(pkg: str, timeout=10):
    # npm registry API doesn't like unencoded '@' in scope for the URL path segment,
    # but requests to registry.npmjs.org/@scope%2Fname work; simpler: use latest endpoint.
    safe = pkg.replace("/", "%2F")
    url = f"https://registry.npmjs.org/{safe}/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "npm-cli-audit-script"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        return data
    except urllib.error.HTTPError as e:
        return {"__error__": f"HTTP {e.code}"}
    except Exception as e:
        return {"__error__": str(e)}


def classify(limit=None):
    if not PACKAGES_CSV.exists():
        print(f"Missing {PACKAGES_CSV}, run extract phase first.")
        sys.exit(1)

    rows = []
    with PACKAGES_CSV.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if limit:
        rows = rows[:limit]

    results = []
    for i, row in enumerate(rows, start=1):
        pkg = row["package"]
        info = fetch_registry_info(pkg)
        if "__error__" in info:
            classification = "unknown"
            has_bin = False
            description = ""
            keywords = ""
            error = info["__error__"]
        else:
            error = ""
            bin_field = info.get("bin")
            has_bin = bool(bin_field)
            description = (info.get("description") or "")[:150]
            keywords = ",".join(info.get("keywords") or [])
            is_cli_by_keyword = any(
                "cli" in k.lower() for k in (info.get("keywords") or [])
            )
            is_cli_by_name = pkg.lower().endswith("-cli") or pkg.lower().startswith("cli-")
            is_cli_by_desc = bool(re.search(r"\bcli\b|command[- ]line", description, re.IGNORECASE))
            if has_bin:
                classification = "cli"
            elif is_cli_by_keyword or is_cli_by_name or is_cli_by_desc:
                classification = "likely-cli (no bin field found)"
            else:
                classification = "library"

        results.append({
            "package": pkg,
            "mentions": row["mentions"],
            "classification": classification,
            "has_bin": has_bin,
            "description": description,
            "keywords": keywords,
            "error": error,
        })

        print(f"[{i}/{len(rows)}] {pkg} -> {classification}")
        time.sleep(0.05)  # be polite to the registry

    with CLASSIFIED_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "package", "mentions", "classification", "has_bin",
            "description", "keywords", "error",
        ])
        writer.writeheader()
        writer.writerows(results)

    cli_count = sum(1 for r in results if r["classification"] == "cli")
    print(f"\nClassified {len(results)} packages: {cli_count} confirmed CLIs.")
    print(f"Written to {CLASSIFIED_CSV}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("extract", "classify"):
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "extract":
        extract()
    elif mode == "classify":
        limit = None
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            limit = int(sys.argv[idx + 1])
        classify(limit=limit)


if __name__ == "__main__":
    main()
