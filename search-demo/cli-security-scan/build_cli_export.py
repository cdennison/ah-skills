#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["qdrant-client"]
# ///
"""Turn the CLI-security reports into a `cli_security` verdict per skill.

Default: write the verdict onto every matched skill's Qdrant point
(`set_payload("agent_skills", {"cli_security": {...}})`) -- the same way
publish_scans.py / the llm_scan step persist their results. This is the
step's only Qdrant write.

    build_cli_export.py                 # write cli_security onto agent_skills
    build_cli_export.py --force         # ignore the same-day rescan gate
    build_cli_export.py --dry-run       # print what would change, write nothing
    build_cli_export.py --csv           # no Qdrant; write work/skills_export_cli.csv

Inputs (from ./run.sh): work/{npm,pip}_security_report_with_skills.csv and
work/install_mentions.log. See ../docs/ARCHITECTURE_CLI_SECURITY_SCAN.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from _common import WORK, osv_snapshot_date  # noqa: E402
from extract_packages import extract_packages_from_line  # noqa: E402
from skill_id_util import skill_id_from_path  # noqa: E402

csv.field_size_limit(10_000_000)

SKILLS_EXPORT = ROOT / "skills_export.csv"
CSV_OUT = WORK / "skills_export_cli.csv"

GRADE_RANK = {"A": 0, "B": 1, "C": 2}


def grade_for_package(vuln_count, max_severity: str) -> str:
    try:
        vuln_count = int(vuln_count)
    except (TypeError, ValueError):
        vuln_count = 0
    if not vuln_count:
        return "A"
    sev = (max_severity or "").upper()
    if sev in ("CRITICAL", "HIGH"):
        return "C"
    if sev in ("MODERATE", "MEDIUM", "LOW"):
        return "B"
    # advisory present but OSV gave no recognized severity label (often a raw
    # CVSS vector) -- treat conservatively as the worst grade, don't assume minor.
    return "C"


def worst_grade(grades) -> str:
    return max(grades, key=lambda g: GRADE_RANK[g], default="A")


def load_reports() -> tuple[dict, dict]:
    """(pkg_info, skill_to_packages).

    pkg_info: {(ecosystem, package): {classification, vuln_count, max_severity, advisory_ids}}
    skill_to_packages: {skill_id: set[(ecosystem, package)]}
    """
    pkg_info: dict[tuple[str, str], dict] = {}
    skill_to_packages: dict[str, set] = defaultdict(set)
    for ecosystem in ("npm", "pip"):
        report = WORK / f"{ecosystem}_security_report_with_skills.csv"
        if not report.exists():
            sys.exit(f"Missing {report} -- run ./run.sh first.")
        with report.open(newline="") as f:
            for row in csv.DictReader(f):
                pkg = row["package"]
                key = (ecosystem, pkg)
                try:
                    vuln_count = int(row.get("vuln_count") or 0)
                except ValueError:
                    vuln_count = 0
                pkg_info[key] = {
                    "classification": row.get("classification", ""),
                    "vuln_count": vuln_count,
                    "max_severity": (row.get("max_severity") or "").upper() or "NONE",
                    "advisory_ids": [a for a in row.get("advisory_ids", "").split(",") if a],
                }
                for sid in (s for s in row.get("skills_mentioning", "").split(",") if s):
                    skill_to_packages[sid].add(key)
    return pkg_info, dict(skill_to_packages)


def load_install_commands(wanted: set[tuple[str, str]]) -> dict[tuple[str, str, str], str]:
    """{(skill_id, ecosystem, package): first literal install line seen}."""
    commands: dict[tuple[str, str, str], str] = {}
    log = WORK / "install_mentions.log"
    with log.open(errors="ignore") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if ":" not in raw:
                continue
            path_str, _, rest = raw.partition(":")
            _, _, line = rest.partition(":")
            line = line.strip()
            skill_id = skill_id_from_path(path_str)
            for ecosystem in ("npm", "pip"):
                for pkg in extract_packages_from_line(line, ecosystem):
                    key = (skill_id, ecosystem, pkg)
                    if (ecosystem, pkg) in wanted and key not in commands:
                        commands[key] = line[:200]
    return commands


def build_verdict(skill_ids, skill_to_packages, pkg_info, commands, snapshot_date) -> dict | None:
    packages = sorted({p for sid in skill_ids for p in skill_to_packages.get(sid, ())})
    if not packages:
        return None
    entries, grades = [], []
    for ecosystem, pkg in packages:
        info = pkg_info.get((ecosystem, pkg), {"classification": "", "vuln_count": 0,
                                               "max_severity": "NONE", "advisory_ids": []})
        cmd = next((commands[(sid, ecosystem, pkg)] for sid in skill_ids
                    if (sid, ecosystem, pkg) in commands), "")
        entries.append({
            "package": pkg,
            "ecosystem": ecosystem,
            "classification": info["classification"],
            "install_command": cmd,
            "vuln_count": info["vuln_count"],
            "max_severity": info["max_severity"],
            "advisory_ids": info["advisory_ids"],
        })
        grades.append(grade_for_package(info["vuln_count"], info["max_severity"]))
    return {
        "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "osv_snapshot_date": snapshot_date,
        "grade": worst_grade(grades),
        "packages": entries,
    }


def _same_packages(existing: dict, verdict: dict) -> bool:
    def keyset(v):
        return {(p["ecosystem"], p["package"]) for p in v.get("packages", [])}
    return keyset(existing) == keyset(verdict)


# --------------------------------------------------------------------------- #
# Qdrant payload mode
# --------------------------------------------------------------------------- #
def run_payload(force: bool, dry_run: bool) -> int:
    from index_qdrant import COLLECTION, get_client

    pkg_info, skill_to_packages = load_reports()
    commands = load_install_commands(set(pkg_info))
    snapshot_date = osv_snapshot_date()

    client = get_client()
    if not client.collection_exists(COLLECTION):
        sys.exit(f"Qdrant collection {COLLECTION!r} does not exist -- run index_qdrant.py first.")

    today = dt.date.today().isoformat()
    written = skipped = cleared = 0
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, with_payload=["path", "locations", "cli_security"],
            with_vectors=False, limit=1000, offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            paths = [payload.get("path")] + [
                loc.get("path") for loc in (payload.get("locations") or []) if isinstance(loc, dict)
            ]
            skill_ids = {skill_id_from_path(pp) for pp in paths if pp}
            verdict = build_verdict(skill_ids, skill_to_packages, pkg_info, commands, snapshot_date)
            existing = payload.get("cli_security")

            if verdict is None:
                if existing is not None:
                    cleared += 1
                    if not dry_run:
                        client.delete_payload(COLLECTION, keys=["cli_security"], points=[p.id])
                continue

            if (not force and existing
                    and existing.get("osv_snapshot_date") == today
                    and _same_packages(existing, verdict)):
                skipped += 1
                continue

            written += 1
            if not dry_run:
                client.set_payload(COLLECTION, payload={"cli_security": verdict}, points=[p.id])
        if offset is None:
            break

    verb = "would write" if dry_run else "wrote"
    print(f"{verb} {written} / skipped {skipped} / cleared {cleared} "
          f"(osv snapshot {snapshot_date})")
    return 0


# --------------------------------------------------------------------------- #
# Offline CSV mode
# --------------------------------------------------------------------------- #
def run_csv() -> int:
    if not SKILLS_EXPORT.exists():
        sys.exit(f"Missing {SKILLS_EXPORT} -- run export_csv.py first.")
    pkg_info, skill_to_packages = load_reports()
    commands = load_install_commands(set(pkg_info))
    snapshot_date = osv_snapshot_date()

    total = kept = 0
    with SKILLS_EXPORT.open(newline="") as fin, CSV_OUT.open("w", newline="") as fout:
        # skills_export.csv can carry stray NUL bytes from an upstream skill's
        # description (the `contacts` skill); csv raises on those.
        reader = csv.DictReader((line.replace("\x00", "") for line in fin))
        fieldnames = [*reader.fieldnames, "cli", "cli_security_grade", "cli_security_scan"]
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            total += 1
            verdict = build_verdict({skill_id_from_path(row["path"])},
                                    skill_to_packages, pkg_info, commands, snapshot_date)
            if verdict is None:
                continue
            row["cli"] = json.dumps(
                [{"package": e["package"], "ecosystem": e["ecosystem"],
                  "install_command": e["install_command"]} for e in verdict["packages"]],
                ensure_ascii=False,
            )
            row["cli_security_grade"] = verdict["grade"]
            row["cli_security_scan"] = json.dumps(
                [{"package": e["package"], "ecosystem": e["ecosystem"],
                  "vuln_count": e["vuln_count"], "max_severity": e["max_severity"],
                  "advisory_ids": e["advisory_ids"]} for e in verdict["packages"]],
                ensure_ascii=False,
            )
            writer.writerow(row)
            kept += 1
    print(f"wrote {kept} skill rows (of {total}) -> {CSV_OUT}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", action="store_true", help="Offline: write work/skills_export_cli.csv, no Qdrant.")
    parser.add_argument("--force", action="store_true", help="Ignore the same-day rescan gate.")
    parser.add_argument("--dry-run", action="store_true", help="Payload mode: print changes, write nothing.")
    args = parser.parse_args(argv)
    return run_csv() if args.csv else run_payload(args.force, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
