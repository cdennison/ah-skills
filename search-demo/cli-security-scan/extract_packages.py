#!/usr/bin/env python3
"""Extract npm/pip package names from install commands in
work/install_mentions.log, then classify each as a CLI tool vs a plain
library via registry metadata.

    extract_packages.py {npm|pip} extract            -> work/<eco>_packages.csv
    extract_packages.py {npm|pip} classify [--limit N] [--refresh]
                                                     -> work/<eco>_packages_classified.csv

Only *trusted* command text is parsed: a backtick-enclosed span, a line that
starts with an install verb, or the tail of a line after ` -- ` / `&&` / `;`
/ `|` that starts with an install verb (this is what catches the very common
`claude mcp add foo -- npx -y <pkg>` shape). Prose like "treat every skill
like an npm install" is ignored.

CLI signal:
  npm  registry.npmjs.org/<pkg>/latest  -> has a "bin" field            -> "cli"
  pip  pypi.org/pypi/<pkg>/json         -> "Environment :: Console" cls  -> "cli"
  both name ends -cli / starts cli- / description mentions CLI           -> "likely-cli"
       registry 404 / unreachable                                       -> "unknown"
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import urllib.request
from collections import Counter

from _common import ECOSYSTEMS, WORK, cached_json

LOG_FILE = WORK / "install_mentions.log"

# Verbs that make the rest of a line (or a post-operator tail) a trusted
# command. Broader than any single ecosystem's extract regex.
_TRUST_VERB = r"(?:npm|npx|yarn|pnpm|pip3?|pipx|uv|uvx|bunx|bun)\b"
_LEADING_TRUST_RE = re.compile(rf"^\s*\$?\s*(?:sudo\s+)?(?:env\s+\w+=\S+\s+)*{_TRUST_VERB}", re.IGNORECASE)
_OPERATOR_TAIL_RE = re.compile(rf"(?:--|&&|\|\||[;|])\s*((?:env\s+\w+=\S+\s+)*{_TRUST_VERB}.*)", re.IGNORECASE)
_BACKTICK_RE = re.compile(r"`([^`]*?)`")

_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][\w-]*$")

_ECO = {
    "npm": {
        "csv_name": "npm_packages",
        "classified_name": "npm_packages_classified",
        # installer verbs: every non-flag token is a package you're installing
        "install_re": re.compile(
            r"\b(?:npm\s+(?:install|i)|yarn\s+add|pnpm\s+(?:add|install)|bun\s+add)\b(?P<rest>[^\n`]*)",
            re.IGNORECASE,
        ),
        # runner verbs: only the FIRST non-flag token is the package
        # (`npx create-react-app my-app`, `npx playwright install chromium`)
        "runner_re": re.compile(
            r"\b(?:npx|npm\s+exec|pnpm\s+dlx|bunx)\b(?P<rest>[^\n`]*)",
            re.IGNORECASE,
        ),
        # optional @scope/name, optional @version
        "pkg_re": re.compile(r"^(@[a-z0-9][\w.-]*/[a-z0-9][\w.-]*|[a-z0-9][\w.-]*)(@[\w.^~><=|*-]+)?$"),
        "normalize": lambda s: s,
        "stopwords": {
            "-g", "--global", "-d", "--save-dev", "-s", "--save", "--production",
            "-y", "--yes", "-w", "--workspace", "--omit=dev", "--no-save", "--no-fund",
            "and", "the", "a", "an", "then", "install", "installed", "installing",
            "package", "packages", "before", "run", "runs", "running", "npm", "npx",
            "yarn", "pnpm", "add", "i", "exec", "dlx", "bunx", "bun",
        },
        "classified_fields": ["package", "mentions", "classification", "has_bin", "description", "keywords", "error"],
    },
    "pip": {
        "csv_name": "pip_packages",
        "classified_name": "pip_packages_classified",
        "install_re": re.compile(
            r"\b(?:pip3?\s+install|pipx\s+install|uv\s+pip\s+install|uv\s+tool\s+install)\b(?P<rest>[^\n`]*)",
            re.IGNORECASE,
        ),
        "runner_re": re.compile(
            r"\b(?:uvx|pipx\s+run)\b(?P<rest>[^\n`]*)",
            re.IGNORECASE,
        ),
        "pkg_re": re.compile(
            r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9,_-]+\])?"
            r"([=<>!~]=?[\w.*]+(,[=<>!~]=?[\w.*]+)*)?$"
        ),
        "normalize": lambda s: s.lower(),
        "stopwords": {
            "-r", "--requirement", "-e", "--editable", "-u", "--user", "--upgrade",
            "-u", "--no-cache-dir", "--no-deps", "--quiet", "-q", "--index-url",
            "--extra-index-url", "--target", "--pre", "--from",
            "and", "the", "a", "an", "then", "install", "installed", "installing",
            "package", "packages", "before", "run", "runs", "running",
            "pip", "pip3", "pipx", "uv", "uvx", "tool", "python", "python3",
            "requirements.txt", "-r.", ".", "..",
        },
        "classified_fields": ["package", "mentions", "classification", "has_console_classifier", "description", "keywords", "error"],
    },
}


def _trusted_command_snippets(line: str) -> list[str]:
    snippets = [m.group(1) for m in _BACKTICK_RE.finditer(line)]
    if _LEADING_TRUST_RE.match(line):
        snippets.append(line)
    for m in _OPERATOR_TAIL_RE.finditer(line):
        snippets.append(m.group(1))
    return snippets


def _packages_in_rest(rest: str, cfg: dict, ecosystem: str, *, first_only: bool) -> list[str]:
    """Package names from the text after an install/runner verb.

    first_only=True (runner verbs like `npx`, `uvx`): the package is the first
    non-flag token and nothing after it (`npx create-react-app my-app`,
    `uvx ruff check .`). first_only=False (installer verbs): every token that
    parses as a package spec, stopping at the first that doesn't.
    """
    # stop at shell operators / comments / prose punctuation
    rest = re.split(r"[|&;#]|&&|\$\(|[.,](?:\s|$)", rest.strip())[0]
    out: list[str] = []
    for tok in rest.split():
        tok = tok.strip("'\",()")
        if not tok:
            continue
        if _FLAG_RE.match(tok) or tok.startswith("-"):
            continue  # a flag; the package can still follow
        low = tok.lower()
        if low in cfg["stopwords"]:
            if first_only:
                break
            continue
        if tok.startswith(("http://", "https://", "git+")):
            break
        if "requirements.txt" in low or (ecosystem == "pip" and "/" in tok):
            break
        mm = cfg["pkg_re"].match(tok)
        if not mm or len(mm.group(1)) < 2:
            break
        out.append(cfg["normalize"](mm.group(1)))
        if first_only:
            break
    return out


def extract_packages_from_line(line: str, ecosystem: str) -> list[str]:
    cfg = _ECO[ecosystem]
    pkgs: list[str] = []
    for snippet in _trusted_command_snippets(line):
        for m in cfg["install_re"].finditer(snippet):
            pkgs += _packages_in_rest(m.group("rest"), cfg, ecosystem, first_only=False)
        for m in cfg["runner_re"].finditer(snippet):
            pkgs += _packages_in_rest(m.group("rest"), cfg, ecosystem, first_only=True)
    return pkgs


def _iter_log_lines():
    with LOG_FILE.open(errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if ":" not in raw:
                continue
            # "<rel path>:<lineno>: <content>"
            _, _, after_path = raw.partition(":")
            _, _, content = after_path.partition(":")
            yield content.strip()


def cmd_extract(ecosystem: str) -> None:
    if not LOG_FILE.exists():
        sys.exit(f"Missing {LOG_FILE} -- run find_install_mentions.py first.")
    cfg = _ECO[ecosystem]

    counter: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for content in _iter_log_lines():
        for pkg in extract_packages_from_line(content, ecosystem):
            counter[pkg] += 1
            examples.setdefault(pkg, content[:120])

    out = WORK / f"{cfg['csv_name']}.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["package", "mentions", "example"])
        for pkg, n in counter.most_common():
            w.writerow([pkg, n, examples.get(pkg, "")])
    print(f"[{ecosystem}] {len(counter)} unique packages from {sum(counter.values())} mentions -> {out}")


def _registry_request(ecosystem: str, pkg: str) -> urllib.request.Request:
    if ecosystem == "npm":
        url = f"https://registry.npmjs.org/{pkg.replace('/', '%2F')}/latest"
    else:
        url = f"https://pypi.org/pypi/{pkg}/json"
    return urllib.request.Request(url)


def _classify_npm(info: dict, pkg: str) -> dict:
    has_bin = bool(info.get("bin"))
    description = (info.get("description") or "")[:150]
    keywords = info.get("keywords") or []
    cli_kw = any("cli" in k.lower() for k in keywords)
    cli_name = pkg.lower().endswith("-cli") or pkg.lower().startswith("cli-")
    cli_desc = bool(re.search(r"\bcli\b|command[- ]line", description, re.IGNORECASE))
    if has_bin:
        classification = "cli"
    elif cli_kw or cli_name or cli_desc:
        classification = "likely-cli"
    else:
        classification = "library"
    return {
        "classification": classification, "has_bin": has_bin,
        "description": description, "keywords": ",".join(keywords),
    }


def _classify_pip(data: dict, pkg: str) -> dict:
    info = data.get("info", {}) or {}
    classifiers = info.get("classifiers") or []
    has_console = any("Environment :: Console" in c or "Console :: Terminal" in c for c in classifiers)
    description = (info.get("summary") or "")[:150]
    keywords = info.get("keywords") or ""
    cli_kw = bool(keywords) and bool(re.search(r"\bcli\b", keywords, re.IGNORECASE))
    cli_name = pkg.lower().endswith("-cli") or pkg.lower().startswith("cli-")
    cli_desc = bool(re.search(r"\bcli\b|command[- ]line", description, re.IGNORECASE))
    if has_console:
        classification = "cli"
    elif cli_kw or cli_name or cli_desc:
        classification = "likely-cli"
    else:
        classification = "library"
    return {
        "classification": classification, "has_console_classifier": has_console,
        "description": description, "keywords": keywords,
    }


def cmd_classify(ecosystem: str, limit: int | None, refresh: bool) -> None:
    cfg = _ECO[ecosystem]
    src = WORK / f"{cfg['csv_name']}.csv"
    if not src.exists():
        sys.exit(f"Missing {src} -- run `{ecosystem} extract` first.")

    with src.open() as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]

    results = []
    for i, row in enumerate(rows, start=1):
        pkg = row["package"]
        data = cached_json("registry", ecosystem, pkg,
                           lambda p=pkg: _registry_request(ecosystem, p), refresh=refresh)
        if "__error__" in data:
            fields = {"classification": "unknown", "description": "", "keywords": "",
                      "error": data["__error__"]}
            fields["has_bin" if ecosystem == "npm" else "has_console_classifier"] = False
        else:
            fields = (_classify_npm if ecosystem == "npm" else _classify_pip)(data, pkg)
            fields["error"] = ""
        results.append({"package": pkg, "mentions": row["mentions"], **fields})
        if i % 200 == 0:
            print(f"[{ecosystem}] classified {i}/{len(rows)}")

    out = WORK / f"{cfg['classified_name']}.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cfg["classified_fields"])
        w.writeheader()
        w.writerows(results)
    counts = Counter(r["classification"] for r in results)
    print(f"[{ecosystem}] {dict(counts)} -> {out}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ecosystem", choices=ECOSYSTEMS)
    parser.add_argument("phase", choices=("extract", "classify"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refresh", action="store_true", help="Ignore the work/cache/registry/ cache.")
    args = parser.parse_args(argv)

    if args.phase == "extract":
        cmd_extract(args.ecosystem)
    else:
        cmd_classify(args.ecosystem, args.limit, args.refresh)


if __name__ == "__main__":
    main()
