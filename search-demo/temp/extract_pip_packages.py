#!/usr/bin/env python3
"""Extract pip package names mentioned in install commands across all skills,
then classify which ones are CLI tools (vs plain libraries) using PyPI
metadata (classifiers like "Environment :: Console", keywords, description).

Two phases:
  1. extract  - parse install_mentions.log, pull out pip/pip3/pipx install
               commands, extract package names, count occurrences,
               write pip_packages.csv (sorted by frequency).
  2. classify - for each unique package, hit the PyPI JSON API
               (pypi.org/pypi/<pkg>/json) and check classifiers/keywords/
               description for CLI signals. Writes pip_packages_classified.csv.

Usage:
  python3 extract_pip_packages.py extract
  python3 extract_pip_packages.py classify [--limit N]
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
PACKAGES_CSV = HERE / "pip_packages.csv"
CLASSIFIED_CSV = HERE / "pip_packages_classified.csv"

CMD_RE = re.compile(
    r"\b(?:pip3?|pipx|uv\s+pip)\s+install\b(?P<rest>[^\n`]*)",
    re.IGNORECASE,
)

BACKTICK_RE = re.compile(r"`([^`]*?)`")
LEADING_CMD_RE = re.compile(
    r"^\s*\$?\s*(pip3?|pipx|uv\s+pip)\s+install\b",
    re.IGNORECASE,
)

FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][\w-]*$")

# A valid pip package spec: name[extras]==version / name>=version / bare name
PKG_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9,_-]+\])?"
    r"([=<>!~]=?[\w.*]+(,[=<>!~]=?[\w.*]+)*)?$"
)

STOPWORDS = {
    "-r", "--requirement", "-e", "--editable", "-u", "--user",
    "--upgrade", "-U", "--no-cache-dir", "--no-deps", "--quiet", "-q",
    "--index-url", "--extra-index-url", "--target", "--pre",
    "and", "the", "a", "an", "then", "install", "installed", "installing",
    "package", "packages", "before", "run", "runs", "running",
    "pip", "pip3", "pipx", "uv", "python", "python3",
    "requirements.txt", "-r.", ".", "..",
}

# Paths / local install targets we don't want to treat as pypi packages
PATHLIKE_RE = re.compile(r"[./\\]")


def _trusted_command_snippets(line: str):
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
            rest = re.split(r"[|&;#]|&&|\$\(|[.,](?:\s|$)", rest)[0]
            tokens = rest.split()
            for tok in tokens:
                tok = tok.strip("'\",()")
                if not tok:
                    continue
                low = tok.lower()
                if low in STOPWORDS or FLAG_RE.match(tok):
                    continue
                if tok.startswith("-"):
                    continue
                if tok.startswith("http://") or tok.startswith("https://") or tok.startswith("git+"):
                    continue
                if "requirements.txt" in low:
                    continue
                if PATHLIKE_RE.search(tok) and "/" in tok:
                    # looks like a local path (./research-library, /path/to/x)
                    continue
                mm = PKG_RE.match(tok)
                if not mm:
                    continue
                pkg_name = mm.group(1)
                if len(pkg_name) < 2:
                    continue
                pkgs.append(pkg_name.lower())
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

    print(f"Extracted {len(counter)} unique pip packages from {sum(counter.values())} mentions.")
    print(f"Written to {PACKAGES_CSV}")


def fetch_pypi_info(pkg: str, timeout=10):
    url = f"https://pypi.org/pypi/{pkg}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "pip-cli-audit-script"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
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
        data = fetch_pypi_info(pkg)
        if "__error__" in data:
            classification = "unknown"
            has_console_classifier = False
            description = ""
            keywords = ""
            error = data["__error__"]
        else:
            error = ""
            info = data.get("info", {}) or {}
            classifiers = info.get("classifiers") or []
            has_console_classifier = any(
                "Environment :: Console" in c or "Console :: Terminal" in c
                for c in classifiers
            )
            description = (info.get("summary") or "")[:150]
            keywords = info.get("keywords") or ""
            is_cli_by_keyword = bool(re.search(r"\bcli\b", keywords, re.IGNORECASE)) if keywords else False
            is_cli_by_name = pkg.lower().endswith("-cli") or pkg.lower().startswith("cli-")
            is_cli_by_desc = bool(re.search(r"\bcli\b|command[- ]line|command line interface", description, re.IGNORECASE))
            # entry point scripts aren't in PyPI JSON metadata directly, so we
            # rely on classifiers + naming/description heuristics.
            if has_console_classifier:
                classification = "cli"
            elif is_cli_by_keyword or is_cli_by_name or is_cli_by_desc:
                classification = "likely-cli (no console classifier found)"
            else:
                classification = "library"

        results.append({
            "package": pkg,
            "mentions": row["mentions"],
            "classification": classification,
            "has_console_classifier": has_console_classifier,
            "description": description,
            "keywords": keywords,
            "error": error,
        })

        print(f"[{i}/{len(rows)}] {pkg} -> {classification}")
        time.sleep(0.05)

    with CLASSIFIED_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "package", "mentions", "classification", "has_console_classifier",
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
