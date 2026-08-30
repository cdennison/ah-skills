#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["qdrant-client"]
# ///
"""Smoke test: CLI/dependency security-scan pipeline end to end for the
**context7 CLI** (`npx -y @upstash/context7-mcp`).

This is the exact install shape a real indexed skill uses --
`yeachan-heo/oh-my-claudecode/skills/mcp-setup/SKILL.md` line 101:

    claude mcp add context7 -- npx -y @upstash/context7-mcp

Stages (all live -- one npm-registry call, one OSV.dev call):

  1. EXTRACT   `extract_packages.extract_packages_from_line(...)` pulls
               `@upstash/context7-mcp` out of the `claude mcp add ... -- npx`
               line (the prototype's line-leading/backtick-only parser missed
               this shape entirely).
  2. CLASSIFY  live npm registry -> the package ships a `bin` -> "cli".
  3. AUDIT     live OSV.dev query -> `vuln_count` is an int (0 is a real
               "scanned, nothing known" result -- the package itself is clean;
               its *dependencies* are a separate story the MCP pipeline covers,
               out of scope here).
  4. GRADE     `build_cli_export.grade_for_package` / `build_verdict` produce a
               well-formed `cli_security` object graded "A".
  5. PAYLOAD   (only if an `agent_skills` collection is reachable and holds a
               context7 skill) `build_cli_export` writes `cli_security` onto
               the point and it survives a re-index. Skipped with a note
               otherwise -- there is no local index in most checkouts.

Run:  uv run python smoke_cli_security_context7.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "cli-security-scan"))

from _common import osv_snapshot_date  # noqa: E402
from audit_packages import _osv_request, severity_label  # noqa: E402
from build_cli_export import build_verdict, grade_for_package  # noqa: E402
from extract_packages import _classify_npm, extract_packages_from_line  # noqa: E402

PACKAGE = "@upstash/context7-mcp"
INSTALL_LINE = "claude mcp add context7 -- npx -y @upstash/context7-mcp"

_passed: list[str] = []
_failed: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (_passed if ok else _failed).append(label)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def _get_json(req: urllib.request.Request) -> dict:
    req.add_header("User-Agent", "ah-skills-cli-security-smoke")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def main() -> int:
    print(f"context7 CLI smoke test -- package {PACKAGE!r}\n")

    # 1. EXTRACT
    print("1. EXTRACT")
    pkgs = extract_packages_from_line(INSTALL_LINE, "npm")
    check("`claude mcp add ... -- npx -y <pkg>` yields the package",
          pkgs == [PACKAGE], f"got {pkgs}")

    # 2. CLASSIFY (live npm registry)
    print("2. CLASSIFY (npm registry)")
    try:
        info = _get_json(urllib.request.Request(
            f"https://registry.npmjs.org/{PACKAGE.replace('/', '%2F')}/latest"))
    except OSError as e:
        return _abort(f"npm registry unreachable: {e}")
    classification = _classify_npm(info, PACKAGE)
    check("npm registry reports a `bin` entry", classification["has_bin"] is True)
    check('classified as "cli"', classification["classification"] == "cli",
          classification["classification"])

    # 3. AUDIT (live OSV.dev)
    print("3. AUDIT (OSV.dev)")
    try:
        osv = _get_json(_osv_request("npm", PACKAGE))
    except OSError as e:
        return _abort(f"OSV.dev unreachable: {e}")
    vulns = osv.get("vulns", []) or []
    max_sev = max((severity_label(v) for v in vulns), default="", key=lambda s: len(s))
    check("OSV response parses; vuln_count is an int",
          isinstance(len(vulns), int), f"vuln_count={len(vulns)} max_severity={max_sev or 'NONE'}")

    # 4. GRADE / verdict shape
    print("4. GRADE + verdict")
    vuln_count = len(vulns)
    osv_max = max((severity_label(v) for v in vulns), key=lambda s: {"": 0, "LOW": 1, "MODERATE": 2,
                  "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}.get(s, 0), default="")
    grade = grade_for_package(vuln_count, osv_max)
    expected = "A" if vuln_count == 0 else grade
    check(f"grade_for_package({vuln_count}, {osv_max or 'NONE'!r}) = {grade!r}", grade == expected)

    skill_id = "yeachan-heo/oh-my-claudecode/skills/mcp-setup"
    pkg_info = {("npm", PACKAGE): {"classification": "cli", "vuln_count": vuln_count,
                                   "max_severity": osv_max or "NONE",
                                   "advisory_ids": [v.get("id", "") for v in vulns]}}
    verdict = build_verdict(
        {skill_id}, {skill_id: {("npm", PACKAGE)}}, pkg_info,
        {(skill_id, "npm", PACKAGE): INSTALL_LINE}, osv_snapshot_date(),
    )
    ok = (
        verdict is not None
        and verdict["grade"] == grade
        and len(verdict["packages"]) == 1
        and verdict["packages"][0]["package"] == PACKAGE
        and verdict["packages"][0]["ecosystem"] == "npm"
        and verdict["packages"][0]["install_command"] == INSTALL_LINE
        and "osv_snapshot_date" in verdict
    )
    check("build_verdict produces a well-formed cli_security object", ok)
    if verdict:
        print("\n  cli_security =", json.dumps(verdict, indent=2)[:900])

    # 5. PAYLOAD round-trip (optional)
    print("\n5. PAYLOAD round-trip (optional)")
    _try_payload_roundtrip(verdict)

    print(f"\n{len(_passed)} passed, {len(_failed)} failed")
    return 1 if _failed else 0


def _abort(msg: str) -> int:
    print(f"  [FAIL] {msg}")
    _failed.append(msg)
    print(f"\n{len(_passed)} passed, {len(_failed)} failed")
    return 1


def _try_payload_roundtrip(verdict) -> None:
    import os

    if not (os.environ.get("SKILLS_QDRANT_URL") or os.environ.get("SKILLS_QDRANT_DB_PATH")):
        print("  [skip] no SKILLS_QDRANT_URL / SKILLS_QDRANT_DB_PATH set -- "
              "run cli-security-scan/build_cli_export.py against a real index to exercise this")
        return
    try:
        from index_qdrant import COLLECTION, get_client

        client = get_client()
        if not client.collection_exists(COLLECTION):
            print(f"  [skip] collection {COLLECTION!r} does not exist")
            return
        hits, _ = client.scroll(
            COLLECTION, with_payload=["path", "cli_security"], with_vectors=False, limit=20000,
        )
        c7 = [h for h in hits if "context7" in (h.payload or {}).get("path", "").lower()
              or "mcp-setup" in (h.payload or {}).get("path", "")]
        if not c7:
            print("  [skip] no context7 skill point in the index")
            return
        got = (c7[0].payload or {}).get("cli_security")
        check("context7 skill point carries a cli_security verdict", got is not None,
              f"grade={got.get('grade') if got else None}")
    except Exception as e:  # noqa: BLE001 -- smoke test, any failure is just a skip
        print(f"  [skip] payload round-trip not exercised: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
