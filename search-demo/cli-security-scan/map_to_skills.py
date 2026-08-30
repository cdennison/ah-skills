#!/usr/bin/env python3
"""Append a `skills_mentioning` column to work/<eco>_security_report.csv:
the sorted skill-ids (search-raw-relative skill directories) whose files
name an install of that package.

    map_to_skills.py {npm|pip}   ->  work/<eco>_security_report_with_skills.csv
"""

from __future__ import annotations

import argparse
import csv
import sys

from _common import WORK
from extract_packages import extract_packages_from_line
from skill_id_util import skill_id_from_path

LOG_FILE = WORK / "install_mentions.log"


def build_package_to_skills(ecosystem: str) -> dict[str, set[str]]:
    pkg_to_skills: dict[str, set[str]] = {}
    with LOG_FILE.open(errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if ":" not in raw:
                continue
            path_str, _, rest = raw.partition(":")
            _, _, line = rest.partition(":")
            pkgs = extract_packages_from_line(line, ecosystem)
            if not pkgs:
                continue
            skill_id = skill_id_from_path(path_str)
            for pkg in pkgs:
                pkg_to_skills.setdefault(pkg, set()).add(skill_id)
    return pkg_to_skills


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ecosystem", choices=("npm", "pip"))
    args = parser.parse_args(argv)

    report = WORK / f"{args.ecosystem}_security_report.csv"
    if not report.exists():
        sys.exit(f"Missing {report} -- run `audit_packages.py {args.ecosystem}` first.")

    pkg_to_skills = build_package_to_skills(args.ecosystem)

    with report.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = [*reader.fieldnames, "skills_mentioning"]
    for row in rows:
        row["skills_mentioning"] = ",".join(sorted(pkg_to_skills.get(row["package"], ())))

    out = WORK / f"{args.ecosystem}_security_report_with_skills.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    mapped = sum(1 for r in rows if r["skills_mentioning"])
    print(f"[{args.ecosystem}] {mapped}/{len(rows)} audited packages map to >=1 skill -> {out}")


if __name__ == "__main__":
    main()
