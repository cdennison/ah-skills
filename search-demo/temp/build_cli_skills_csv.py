#!/usr/bin/env python3
"""Build a CSV mirroring skills_export.csv, filtered to only skills that
mention installing a confirmed CLI package (npm or pip), with three added
columns:

  cli                 - JSON array of {package, ecosystem, install_command}
  cli_security_grade  - A/B/C, the worst grade among all CLI packages the
                         skill installs (A = no known advisories, B = worst
                         known advisory is LOW/MODERATE, C = worst known
                         advisory is HIGH/CRITICAL or has no OSV severity
                         label at all, which is treated conservatively)
  cli_security_scan   - JSON array of {package, ecosystem, vuln_count,
                         max_severity, advisory_ids}

Sources (already computed, no re-scraping/re-indexing, no Qdrant):
  - cli_security_report_with_skills.csv  (npm, cli/likely-cli only)
  - pip_security_report_with_skills.csv  (pip, cli/likely-cli only)
  - install_mentions.log                 (to recover the literal install
    command text per skill+package, since the report CSVs only aggregate
    package -> skills, not the exact command used at each site)
  - skills_export.csv                    (the row set + all mirrored columns)

Usage:
  python3 build_cli_skills_csv.py
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_npm_packages import extract_packages_from_line as npm_extract
from extract_pip_packages import extract_packages_from_line as pip_extract
from skill_id_util import skill_id_from_path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

NPM_REPORT = HERE / "cli_security_report_with_skills.csv"
PIP_REPORT = HERE / "pip_security_report_with_skills.csv"
LOG_FILE = HERE / "install_mentions.log"
SKILLS_EXPORT = ROOT / "skills_export.csv"
OUT_CSV = HERE / "skills_export_cli.csv"

csv.field_size_limit(10_000_000)

GRADE_RANK = {"A": 0, "B": 1, "C": 2}


def grade_for_package(vuln_count: int, max_severity: str) -> str:
    if not vuln_count:
        return "A"
    sev = (max_severity or "").upper()
    if sev in ("CRITICAL", "HIGH"):
        return "C"
    if sev in ("MODERATE", "MEDIUM", "LOW"):
        return "B"
    # vuln_count > 0 but severity wasn't a recognized category (e.g. OSV
    # gave a raw CVSS vector string instead of a label) -- treat
    # conservatively as the worst grade rather than assume it's minor.
    return "C"


def worst_grade(grades) -> str:
    return max(grades, key=lambda g: GRADE_RANK[g], default="A")


def load_security_info(report_path, ecosystem):
    """Return {package: {vuln_count, max_severity, advisory_ids}} and the
    set of skill_ids (already computed) that mention each package."""
    pkg_info = {}
    pkg_skills = {}
    with report_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pkg = row["package"]
            advisory_ids = [a for a in row.get("advisory_ids", "").split(",") if a]
            vuln_count = row.get("vuln_count", "0")
            try:
                vuln_count = int(vuln_count)
            except ValueError:
                vuln_count = 0
            pkg_info[pkg] = {
                "vuln_count": vuln_count,
                "max_severity": row.get("max_severity", "") or "none",
                "advisory_ids": advisory_ids,
            }
            skills = [s for s in row.get("skills_mentioning", "").split(",") if s]
            pkg_skills[pkg] = set(skills)
    return pkg_info, pkg_skills


def build_skill_package_commands(target_packages_npm, target_packages_pip):
    """Single pass over install_mentions.log: for each (skill_id, package)
    pair whose package is in our confirmed-CLI sets, collect the distinct
    literal command snippets seen."""
    commands = defaultdict(set)  # (skill_id, ecosystem, package) -> {snippet, ...}

    with LOG_FILE.open(errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if ":" not in raw:
                continue
            path_str, _, rest = raw.partition(":")
            _, _, line = rest.partition(":")
            skill_id = skill_id_from_path(path_str)

            npm_pkgs = set(npm_extract(line)) & target_packages_npm
            for pkg in npm_pkgs:
                commands[(skill_id, "npm", pkg)].add(line.strip()[:200])

            pip_pkgs = set(pip_extract(line)) & target_packages_pip
            for pkg in pip_pkgs:
                commands[(skill_id, "pip", pkg)].add(line.strip()[:200])

    return commands


def main():
    if not NPM_REPORT.exists() or not PIP_REPORT.exists():
        print("Missing report CSVs — run the extract/classify/audit/map pipeline first.")
        sys.exit(1)

    npm_info, npm_skills = load_security_info(NPM_REPORT, "npm")
    pip_info, pip_skills = load_security_info(PIP_REPORT, "pip")

    # skill_id -> set of (ecosystem, package)
    skill_to_packages = defaultdict(set)
    for pkg, skills in npm_skills.items():
        for s in skills:
            skill_to_packages[s].add(("npm", pkg))
    for pkg, skills in pip_skills.items():
        for s in skills:
            skill_to_packages[s].add(("pip", pkg))

    print(f"{len(skill_to_packages)} distinct skills mention a confirmed CLI package.")

    commands = build_skill_package_commands(set(npm_info), set(pip_info))

    total = 0
    kept = 0
    with SKILLS_EXPORT.open(newline="") as fin, OUT_CSV.open("w", newline="") as fout:
        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames + ["cli", "cli_security_grade", "cli_security_scan"]
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            total += 1
            skill_id = skill_id_from_path(row["path"])
            pkgs = skill_to_packages.get(skill_id)
            if not pkgs:
                continue

            cli_entries = []
            scan_entries = []
            for ecosystem, pkg in sorted(pkgs):
                cmds = sorted(commands.get((skill_id, ecosystem, pkg), []))
                install_command = cmds[0] if cmds else ""
                cli_entries.append({
                    "package": pkg,
                    "ecosystem": ecosystem,
                    "install_command": install_command,
                })
                info = npm_info.get(pkg) if ecosystem == "npm" else pip_info.get(pkg)
                info = info or {"vuln_count": 0, "max_severity": "none", "advisory_ids": []}
                scan_entries.append({
                    "package": pkg,
                    "ecosystem": ecosystem,
                    **info,
                })

            grades = [grade_for_package(e["vuln_count"], e["max_severity"]) for e in scan_entries]

            row["cli"] = json.dumps(cli_entries, ensure_ascii=False)
            row["cli_security_grade"] = worst_grade(grades)
            row["cli_security_scan"] = json.dumps(scan_entries, ensure_ascii=False)
            writer.writerow(row)
            kept += 1

    print(f"Wrote {kept} skill rows (of {total} total) to {OUT_CSV}")


if __name__ == "__main__":
    main()
