#!/usr/bin/env python3
"""Check confirmed CLI pip packages (from pip_packages_classified.csv) for
known security advisories via OSV.dev (PyPI ecosystem).

Usage:
  python3 audit_pip_packages.py [--all]   # --all also checks "library" rows

Writes pip_security_report.csv.
"""

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLASSIFIED_CSV = HERE / "pip_packages_classified.csv"
REPORT_CSV = HERE / "pip_security_report.csv"

OSV_URL = "https://api.osv.dev/v1/query"

SEVERITY_ORDER = {"": 0, "LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def osv_query(pkg: str):
    payload = {"package": {"name": pkg, "ecosystem": "PyPI"}}
    req = urllib.request.Request(
        OSV_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return {"__error__": f"HTTP {e.code}"}
    except Exception as e:
        return {"__error__": str(e)}


def severity_of(vuln):
    sev = ""
    for s in vuln.get("severity", []) or []:
        t = s.get("type", "")
        score = s.get("score", "")
        if "CVSS" in t:
            sev = score
    db_specific = vuln.get("database_specific", {}) or {}
    label = db_specific.get("severity", "")
    return label or sev


def main():
    include_all = "--all" in sys.argv

    if not CLASSIFIED_CSV.exists():
        print(f"Missing {CLASSIFIED_CSV} — run extract_pip_packages.py classify first.")
        sys.exit(1)

    rows = []
    with CLASSIFIED_CSV.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not include_all and row["classification"] not in ("cli", "likely-cli (no console classifier found)"):
                continue
            rows.append(row)

    print(f"Auditing {len(rows)} packages via OSV.dev (PyPI)...")

    results = []
    for i, row in enumerate(rows, start=1):
        pkg = row["package"]
        data = osv_query(pkg)
        if "__error__" in data:
            results.append({**row, "vuln_count": "?", "max_severity": "", "advisory_ids": "", "summary": data["__error__"]})
            print(f"[{i}/{len(rows)}] {pkg} -> error: {data['__error__']}")
            time.sleep(0.1)
            continue

        vulns = data.get("vulns", []) or []
        ids = [v.get("id", "") for v in vulns]
        severities = [severity_of(v).upper() for v in vulns]
        max_sev = max(severities, key=lambda s: SEVERITY_ORDER.get(s, 0), default="")
        summaries = "; ".join((v.get("summary") or "")[:100] for v in vulns[:3])

        results.append({
            **row,
            "vuln_count": len(vulns),
            "max_severity": max_sev,
            "advisory_ids": ",".join(ids),
            "summary": summaries,
        })
        flag = f" -> {len(vulns)} advisories ({max_sev})" if vulns else " -> clean"
        print(f"[{i}/{len(rows)}] {pkg}{flag}")
        time.sleep(0.1)

    with REPORT_CSV.open("w", newline="") as f:
        fieldnames = ["package", "mentions", "classification", "has_console_classifier",
                      "vuln_count", "max_severity", "advisory_ids", "summary"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    flagged = [r for r in results if isinstance(r["vuln_count"], int) and r["vuln_count"] > 0]
    flagged.sort(key=lambda r: SEVERITY_ORDER.get(r["max_severity"], 0), reverse=True)

    print(f"\n{len(flagged)} of {len(results)} packages have known advisories.")
    print(f"Full report: {REPORT_CSV}\n")
    if flagged:
        print("Top flagged packages:")
        for r in flagged[:20]:
            print(f"  {r['package']} ({r['mentions']} mentions) — {r['vuln_count']} advisories, max severity {r['max_severity']}: {r['summary']}")


if __name__ == "__main__":
    main()
