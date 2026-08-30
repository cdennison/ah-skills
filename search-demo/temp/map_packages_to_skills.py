#!/usr/bin/env python3
"""Map each npm package (extracted from install_mentions.log) to the set of
skills that mention installing it, then append that as a comma-delimited
last column on cli_security_report.csv.

Usage:
  python3 map_packages_to_skills.py
"""

import csv
from pathlib import Path

from extract_npm_packages import extract_packages_from_line
from skill_id_util import skill_id_from_path

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "install_mentions.log"
REPORT_CSV = HERE / "cli_security_report.csv"
OUT_CSV = HERE / "cli_security_report_with_skills.csv"


def build_package_to_skills():
    pkg_to_skills = {}

    with LOG_FILE.open(errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if ":" not in raw:
                continue
            path_str, _, rest = raw.partition(":")
            # rest is "<lineno>: <line content>"
            lineno_str, _, line = rest.partition(":")
            pkgs = extract_packages_from_line(line)
            if not pkgs:
                continue
            skill_id = skill_id_from_path(path_str)
            for pkg in pkgs:
                pkg_to_skills.setdefault(pkg, set()).add(skill_id)

    return pkg_to_skills


def main():
    if not REPORT_CSV.exists():
        print(f"Missing {REPORT_CSV} — run audit_cli_packages.py first.")
        return

    pkg_to_skills = build_package_to_skills()

    with REPORT_CSV.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames + ["skills_mentioning"]

    for row in rows:
        skills = sorted(pkg_to_skills.get(row["package"], []))
        row["skills_mentioning"] = ",".join(skills)

    with OUT_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows with skills_mentioning column to {OUT_CSV}")


if __name__ == "__main__":
    main()
