#!/usr/bin/env python3
"""Audit classified CLI packages against OSV.dev.

    audit_packages.py {npm|pip} [--all] [--refresh]

Reads work/<eco>_packages_classified.csv, queries OSV.dev for every row
classified "cli" or "likely-cli" (--all also does "library"; "unknown" is
always skipped), and writes work/<eco>_security_report.csv:

    package, mentions, classification, vuln_count, max_severity, advisory_ids, summary

OSV is queried WITHOUT a version, so vuln_count is "advisories ever filed
against this package across all versions", not "this install is vulnerable".
Most install commands don't pin a version -- see the design doc's
"Why 'package has an advisory'" section. Responses are cached under
work/cache/osv/.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request

from _common import SEVERITY_ORDER, WORK, cached_json

OSV_URL = "https://api.osv.dev/v1/query"
_OSV_ECOSYSTEM = {"npm": "npm", "pip": "PyPI"}
_AUDITED_CLASSES = {"cli", "likely-cli"}

_HAS_COLUMN = {"npm": "has_bin", "pip": "has_console_classifier"}


def _osv_request(ecosystem: str, pkg: str) -> urllib.request.Request:
    payload = {"package": {"name": pkg, "ecosystem": _OSV_ECOSYSTEM[ecosystem]}}
    return urllib.request.Request(
        OSV_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )


def severity_label(vuln: dict) -> str:
    """OSV puts severity in several shapes; prefer an explicit label, fall
    back to a CVSS string (which SEVERITY_ORDER ranks as 0 -> caller treats
    conservatively)."""
    db_specific = vuln.get("database_specific", {}) or {}
    if label := db_specific.get("severity"):
        return str(label).upper()
    for s in vuln.get("severity", []) or []:
        if "CVSS" in s.get("type", ""):
            return str(s.get("score", "")).upper()
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ecosystem", choices=("npm", "pip"))
    parser.add_argument("--all", action="store_true", help="Also audit rows classified 'library'.")
    parser.add_argument("--refresh", action="store_true", help="Ignore the work/cache/osv/ cache.")
    args = parser.parse_args(argv)

    src = WORK / f"{args.ecosystem}_packages_classified.csv"
    if not src.exists():
        sys.exit(f"Missing {src} -- run `extract_packages.py {args.ecosystem} classify` first.")

    with src.open() as f:
        rows = [
            r for r in csv.DictReader(f)
            if args.all or r["classification"] in _AUDITED_CLASSES
        ]
    print(f"[{args.ecosystem}] auditing {len(rows)} packages via OSV.dev...")

    results = []
    flagged = 0
    for i, row in enumerate(rows, start=1):
        pkg = row["package"]
        data = cached_json("osv", args.ecosystem, pkg,
                           lambda p=pkg: _osv_request(args.ecosystem, p), refresh=args.refresh)
        if "__error__" in data:
            results.append({**row, "vuln_count": "?", "max_severity": "",
                            "advisory_ids": "", "summary": data["__error__"]})
            continue

        vulns = data.get("vulns", []) or []
        severities = [severity_label(v) for v in vulns]
        max_sev = max(severities, key=lambda s: SEVERITY_ORDER.get(s, 0), default="")
        results.append({
            **row,
            "vuln_count": len(vulns),
            "max_severity": max_sev,
            "advisory_ids": ",".join(v.get("id", "") for v in vulns),
            "summary": "; ".join((v.get("summary") or "")[:100] for v in vulns[:3]),
        })
        if vulns:
            flagged += 1
        if i % 200 == 0:
            print(f"[{args.ecosystem}] audited {i}/{len(rows)}")

    out = WORK / f"{args.ecosystem}_security_report.csv"
    fields = ["package", "mentions", "classification", _HAS_COLUMN[args.ecosystem],
              "vuln_count", "max_severity", "advisory_ids", "summary"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    print(f"[{args.ecosystem}] {flagged}/{len(results)} packages have >=1 advisory -> {out}")


if __name__ == "__main__":
    main()
