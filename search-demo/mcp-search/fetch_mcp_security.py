#!/usr/bin/env python3
"""Backfill known-vulnerability data onto mcp-repo-seeds/registry.json via
OSV.dev (osv.dev) -- the same defensive-security lookup GitHub's own
Dependabot/Security Advisories feed into, aggregating GHSA/CVE/PYSEC/etc.
records per package+ecosystem. Public, unauthenticated, no code execution
involved (unlike `npm audit`/`pip-audit`, which require actually installing
the package) -- a pure metadata lookup by package name, matching this
pipeline's existing pattern of never running untrusted server code.

Covers npm and pypi rows today (the only two `registry_type`s OSV has a
matching ecosystem for out of what this pipeline tracks: `oci`/`nuget`/
`cargo`/`crates`/`mcpb` aren't in OSV's ecosystem list). For each eligible
row, POST https://api.osv.dev/v1/query with {"package": {"name":
<package_identifier>, "ecosystem": "npm"|"PyPI"}} -- name-only (no
"version"), so results cover every vulnerability ever reported against the
package across all versions, not just whichever version this registry
happens to have recorded ("has this package ever had a known vuln" is the
useful triage signal here, not "is the exact currently-tracked version
vulnerable," since this pipeline doesn't reliably track installed versions
per row). No auth, paced via shared.http.default_limiter() -- OSV publishes
no strict documented rate limit for /v1/query, but this pipeline defaults
to being a polite, conservative citizen of every API it touches, same as
everywhere else.

Stores security_vuln_count, security_vuln_ids (list), and
security_max_severity (highest of any OSV `database_specific.severity`
label found among the results -- CRITICAL > HIGH > MODERATE > LOW; absent
if OSV had no severity label for any finding, which is common for
PYSEC-sourced entries) -- see mcp_registry.set_security_scan(). A clean
package (zero vulns) is recorded as a real, meaningful zero, not skipped.

At today's scale (~7.2K npm + ~3K pypi rows) this is small enough to run
inline; OSV's batch endpoint (/v1/querybatch, up to 1000 packages/call)
would be the natural next step if this pipeline scales far past that --
deliberately not built yet since /v1/querybatch's response omits
summary/severity (only id + modified), needing a second per-vuln-id lookup
to get anything worth storing, which is real extra complexity not justified
at current scale. Not built speculatively.

DEPENDENCY COVERAGE -- direct only, not transitive: this also fetches each
package's DECLARED direct dependencies straight from its own manifest
(npm registry.npmjs.org/<pkg>/latest's `dependencies`; PyPI's
pypi.org/pypi/<pkg>/json's `info.requires_dist`, extras/env-marker-gated
entries excluded since a plain install doesn't pull those in) and checks
each of THOSE by name against OSV too -- see fetch_direct_dependencies()/
fetch_osv_scan_with_deps(). The dependency pass records
security_direct_deps_scanned/vuln_count/with_vulns, plus
security_direct_deps_max_severity (highest severity label seen across ALL
dep advisories -- distinct from security_max_severity, which stays
package-own) and security_direct_deps_vuln_ids (deduped, capped at
MAX_DEP_VULN_IDS). For a package that is itself clean but has vulnerable
dependencies (the common supply-chain case -- e.g. @upstash/context7-mcp:
0 own vulns, 44 across 4 of 8 direct deps) the entire security story is in
those dep fields, so they're written unconditionally on every with-deps
scan (empty list / null severity is a real "checked, nothing", not a skip).
This is genuinely NOT a full dependency tree:
no version-range resolution, no transitive walk past depth 1 (a
dependency's own dependencies are never fetched). Going further would mean
either reimplementing npm/pip's semver resolver (its own substantial
project, still imprecise without a real lockfile) or actually installing
the package to get one (`npm install`/`pip install`) -- which this
pipeline has never done anywhere, deliberately, specifically to avoid
executing untrusted server code (see module intro). Direct-dependency
coverage is the honest ceiling reachable by pure metadata lookup; deeper
coverage is a real, larger, and NOT-yet-built next step, not something
silently approximated here.

Usage:
    python fetch_mcp_security.py                       # full run, all eligible rows
    python fetch_mcp_security.py --limit 50             # quick test
    python fetch_mcp_security.py --random-sample 10      # N random eligible rows (for review before scaling up)
    python fetch_mcp_security.py --ids github:owner/repo,github:owner2/repo2   # targeted re-run
    python fetch_mcp_security.py --rescan                # ignore --stale-days freshness check
"""

import argparse
import datetime
import random
import re
import sys
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from export_mcp_csv import first_descriptor_value
from shared.http import default_limiter, get_json, post_json

SAVE_EVERY = 200
DEFAULT_STALE_DAYS = 30  # vuln disclosures are far less time-sensitive than
# star/download counts -- a month-old "no known vulns" result is still
# useful triage signal, so this defaults much longer than
# fetch_mcp_rankings.py's 7-day window.

OSV_URL = "https://api.osv.dev/v1/query"
REGISTRY_TYPE_TO_OSV_ECOSYSTEM = {"npm": "npm", "pypi": "PyPI"}
SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "LOW": 1}
MAX_DIRECT_DEPS_SCANNED = 40  # sanity cap, not a real-world limiter -- an
# MCP server package normally declares single digits of direct deps; this
# only guards against a pathological/malformed manifest turning one row
# into dozens of extra OSV calls.
MAX_DEP_VULN_IDS = 60  # cap on security_direct_deps_vuln_ids -- a triage
# pointer ("which advisories are downstream"), not an exhaustive audit
# feed; a handful of vulnerable deps can already carry dozens of advisory
# ids each and the full list belongs in an OSV lookup, not this payload.
_PYPI_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def is_stale(updated_iso: str | None, stale_days: int) -> bool:
    if not updated_iso:
        return True
    try:
        updated = datetime.datetime.fromisoformat(updated_iso)
    except ValueError:
        return True
    return (datetime.datetime.now() - updated) > datetime.timedelta(days=stale_days)


def fetch_osv_scan(pkg: str, ecosystem: str, limiter) -> dict:
    """One OSV query -> {security_vuln_count, security_vuln_ids,
    security_max_severity?}. security_max_severity is only included when at
    least one result carries a database_specific.severity label -- absence
    means "unknown," never a fabricated default."""
    data = post_json(OSV_URL, limiter, {"package": {"name": pkg, "ecosystem": ecosystem}})
    vulns = data.get("vulns", [])
    ids = [v.get("id") for v in vulns if v.get("id")]

    best_severity = None
    best_rank = 0
    for v in vulns:
        label = (v.get("database_specific") or {}).get("severity")
        rank = SEVERITY_RANK.get(label, 0)
        if rank > best_rank:
            best_rank, best_severity = rank, label

    result = {"security_vuln_count": len(ids), "security_vuln_ids": ids}
    if best_severity is not None:
        result["security_max_severity"] = best_severity
    return result


def fetch_direct_dependencies(pkg: str, ecosystem: str, limiter) -> list[str]:
    """Direct (declared, unresolved) dependency names for `pkg`, straight
    off its own published manifest -- no version-range solving, no install,
    no lockfile. See module docstring's "DEPENDENCY COVERAGE" section for
    why this stops at depth 1. Returns [] (not an error) for a package with
    no manifest / no dependencies / a 404 -- a package genuinely having
    zero dependencies is common and not a fetch failure."""
    try:
        if ecosystem == "npm":
            data = get_json(f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@/')}/latest", limiter)
            return list((data.get("dependencies") or {}).keys())[:MAX_DIRECT_DEPS_SCANNED]
        if ecosystem == "PyPI":
            data = get_json(f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json", limiter)
            names = []
            for spec in (data.get("info") or {}).get("requires_dist") or []:
                if ";" in spec:
                    continue  # extras/environment-marker-gated -- not installed by a plain `pip install <pkg>`
                match = _PYPI_DEP_NAME_RE.match(spec.strip())
                if match:
                    names.append(match.group(0))
            return names[:MAX_DIRECT_DEPS_SCANNED]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    return []


def fetch_osv_scan_with_deps(pkg: str, ecosystem: str, limiter, fallback_deps: list[str] | None = None) -> dict:
    """fetch_osv_scan() on `pkg` itself, PLUS a pass over its direct
    dependencies (fetch_direct_dependencies()) -- each dependency name is
    checked against OSV the same way `pkg` is. Adds these keys to the
    result, all written unconditionally (an empty/null value is a real
    "scanned, nothing found," never a skip):
      - security_direct_deps_scanned    (int)       how many dep names checked
      - security_direct_deps_vuln_count (int)       total advisories across them
      - security_direct_deps_with_vulns (list[str]) WHICH deps carry vulns --
          named, not just counted, so a human knows where to look
      - security_direct_deps_max_severity (str|None) highest OSV severity
          label seen across ALL dep advisories (CRITICAL>HIGH>MODERATE>LOW),
          None if none of them carried a label. Deliberately SEPARATE from
          security_max_severity (which stays "the package's own"): for a
          clean package with vulnerable deps, security_max_severity is null
          and this is where the real severity signal lives.
      - security_direct_deps_vuln_ids   (list[str]) the dep advisory ids,
          deduped, capped at MAX_DEP_VULN_IDS -- a triage pointer, not an
          exhaustive feed.

    `fallback_deps` (enrich_from_repo_scan.py's `pyproject_dependencies`) is
    used when the package manager registry has no manifest to read -- i.e. a
    server that ships a pyproject.toml but was never actually published to
    PyPI. The package's own OSV query then legitimately finds nothing, but
    its declared direct deps are still real and still worth checking; this
    is the "scan the deps even when the thing itself isn't a package" case."""
    own = fetch_osv_scan(pkg, ecosystem, limiter)

    dep_names = fetch_direct_dependencies(pkg, ecosystem, limiter)
    if not dep_names and fallback_deps:
        dep_names = list(dict.fromkeys(fallback_deps))[:MAX_DIRECT_DEPS_SCANNED]
    deps_with_vulns = []
    dep_vuln_total = 0
    dep_vuln_ids: list[str] = []
    deps_best_severity = None
    deps_best_rank = 0
    for dep_name in dep_names:
        dep_scan = fetch_osv_scan(dep_name, ecosystem, limiter)
        if dep_scan["security_vuln_count"] > 0:
            deps_with_vulns.append(dep_name)
            dep_vuln_total += dep_scan["security_vuln_count"]
            dep_vuln_ids.extend(dep_scan["security_vuln_ids"])
            # dep_scan only carries security_max_severity when at least one
            # of that dep's advisories had a label (see fetch_osv_scan) --
            # absence contributes rank 0, never a fabricated floor.
            rank = SEVERITY_RANK.get(dep_scan.get("security_max_severity"), 0)
            if rank > deps_best_rank:
                deps_best_rank, deps_best_severity = rank, dep_scan["security_max_severity"]

    own["security_direct_deps_scanned"] = len(dep_names)
    own["security_direct_deps_vuln_count"] = dep_vuln_total
    own["security_direct_deps_with_vulns"] = deps_with_vulns
    own["security_direct_deps_max_severity"] = deps_best_severity
    own["security_direct_deps_vuln_ids"] = list(dict.fromkeys(dep_vuln_ids))[:MAX_DEP_VULN_IDS]
    return own


def fetch_security(registry, index, limiter, *, limit, random_sample, rescan, stale_days, only_ids=None,
                    with_deps=True) -> None:
    candidates = []
    for r in registry:
        if only_ids is not None and r["id"] not in only_ids:
            continue
        registry_type = first_descriptor_value(r, "registry_type")
        ecosystem = REGISTRY_TYPE_TO_OSV_ECOSYSTEM.get(registry_type)
        if ecosystem is None:
            continue
        pkg = first_descriptor_value(r, "package_identifier")
        if not pkg:
            continue
        if only_ids is None and not rescan and not is_stale(r.get("security_updated"), stale_days):
            continue
        fallback_deps = first_descriptor_value(r, "pyproject_dependencies")
        candidates.append((r["id"], pkg, ecosystem, fallback_deps))

    if random_sample is not None:
        candidates = random.sample(candidates, min(random_sample, len(candidates)))
    elif limit is not None:
        candidates = candidates[:limit]

    print(f"[security] {len(candidates)} row(s) to scan")
    ok = failed = with_vulns = 0
    for i, (entry_id, pkg, ecosystem, fallback_deps) in enumerate(candidates, start=1):
        try:
            scan = (
                fetch_osv_scan_with_deps(pkg, ecosystem, limiter, fallback_deps=fallback_deps)
                if with_deps
                else fetch_osv_scan(pkg, ecosystem, limiter)
            )
            mcp_registry.set_security_scan(registry, entry_id, scan, index=index)
            ok += 1
            if scan["security_vuln_count"] > 0:
                with_vulns += 1
        except urllib.error.HTTPError as e:
            mcp_registry.record_error(registry, entry_id, "osv_security", f"{e.code} {e.reason}", index=index)
            failed += 1
        except Exception as e:
            mcp_registry.record_error(registry, entry_id, "osv_security", repr(e), index=index)
            failed += 1
        if i % SAVE_EVERY == 0:
            print(f"  [security] {i}/{len(candidates)} ({ok} ok, {with_vulns} with known vulns, {failed} failed)")
            mcp_registry.save_registry(registry)
    mcp_registry.save_registry(registry)
    print(f"[security] done: {ok} ok ({with_vulns} with known vulns), {failed} failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Cap rows scanned (testing)")
    parser.add_argument(
        "--random-sample", type=int, default=None, metavar="N",
        help="Scan N randomly chosen eligible rows instead of the full/limited set -- "
        "for a representative small batch to review before scaling up.",
    )
    parser.add_argument("--rescan", action="store_true", help="Refresh every eligible row regardless of freshness")
    parser.add_argument(
        "--stale-days", type=int, default=DEFAULT_STALE_DAYS,
        help=f"Skip rows scanned within this many days unless --rescan (default {DEFAULT_STALE_DAYS})",
    )
    parser.add_argument(
        "--ids", type=str, default=None,
        help="Comma-separated registry ids to scan (ignores freshness gating, always rescans exactly these rows).",
    )
    parser.add_argument(
        "--no-deps", action="store_true",
        help="Skip the direct-dependency pass (fetch_osv_scan_with_deps) -- scan only the package itself. "
        "Faster, but see module docstring's 'DEPENDENCY COVERAGE' note for what this loses.",
    )
    args = parser.parse_args()

    registry = mcp_registry.load_registry()
    index = mcp_registry.build_index(registry)
    only_ids = set(args.ids.split(",")) if args.ids else None

    fetch_security(
        registry, index, default_limiter(),
        limit=args.limit, random_sample=args.random_sample, rescan=args.rescan, stale_days=args.stale_days,
        only_ids=only_ids, with_deps=not args.no_deps,
    )


if __name__ == "__main__":
    main()
