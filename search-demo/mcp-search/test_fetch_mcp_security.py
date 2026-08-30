"""Unit coverage for fetch_mcp_security's direct-dependency OSV pass
(E2E Test Plan 03, Findings 7 & 8): the dependency scan must aggregate the
highest severity seen across ALL dep advisories and collect their ids,
without touching the package-own `security_max_severity`.

Hermetic -- no network: `post_json` (OSV) and `get_json` (npm/PyPI
manifest) are replaced with in-memory routers keyed by package name.
"""

import fetch_mcp_security


def _osv_vulns(*specs):
    """specs: (id, severity|None) -> an OSV /v1/query response body."""
    return {
        "vulns": [
            {"id": vid, **({"database_specific": {"severity": sev}} if sev else {})}
            for vid, sev in specs
        ]
    }


def _install(monkeypatch, *, deps, osv):
    """deps: {pkg: {depname: range}} manifests. osv: {name: response body}."""

    def fake_get_json(url, limiter, **kwargs):
        # only the npm manifest path is exercised here
        name = url.split("registry.npmjs.org/", 1)[1].rsplit("/latest", 1)[0]
        name = name.replace("%2F", "/")
        return {"dependencies": deps.get(name, {})}

    def fake_post_json(url, limiter, payload):
        name = payload["package"]["name"]
        return osv.get(name, {"vulns": []})

    monkeypatch.setattr(fetch_mcp_security, "get_json", fake_get_json)
    monkeypatch.setattr(fetch_mcp_security, "post_json", fake_post_json)


def test_dep_pass_takes_max_severity_across_two_deps(monkeypatch):
    """rootpkg itself is clean; dep-a maxes at MODERATE, dep-b at CRITICAL
    -> security_direct_deps_max_severity is CRITICAL (the max wins), and
    the package-own security_max_severity is absent (rootpkg has no vulns
    at all, so no label to report)."""
    _install(
        monkeypatch,
        deps={"rootpkg": {"dep-a": "^1", "dep-b": "^2", "dep-c": "^3"}},
        osv={
            "rootpkg": {"vulns": []},
            "dep-a": _osv_vulns(("GHSA-a1", "MODERATE"), ("GHSA-a2", "LOW")),
            "dep-b": _osv_vulns(("GHSA-b1", "CRITICAL")),
            "dep-c": {"vulns": []},
        },
    )

    scan = fetch_mcp_security.fetch_osv_scan_with_deps("rootpkg", "npm", limiter=None)

    assert scan["security_vuln_count"] == 0
    assert "security_max_severity" not in scan  # package-own: no label, not fabricated
    assert scan["security_direct_deps_scanned"] == 3
    assert scan["security_direct_deps_vuln_count"] == 3
    assert scan["security_direct_deps_with_vulns"] == ["dep-a", "dep-b"]
    assert scan["security_direct_deps_max_severity"] == "CRITICAL"
    assert scan["security_direct_deps_vuln_ids"] == ["GHSA-a1", "GHSA-a2", "GHSA-b1"]


def test_dep_pass_does_not_overload_package_own_severity(monkeypatch):
    """rootpkg has its own HIGH advisory; a dep has CRITICAL. The two
    severities are reported SEPARATELY -- security_max_severity stays HIGH
    (package-own), security_direct_deps_max_severity is CRITICAL."""
    _install(
        monkeypatch,
        deps={"rootpkg": {"dep-a": "^1"}},
        osv={
            "rootpkg": _osv_vulns(("GHSA-root", "HIGH")),
            "dep-a": _osv_vulns(("GHSA-a1", "CRITICAL")),
        },
    )

    scan = fetch_mcp_security.fetch_osv_scan_with_deps("rootpkg", "npm", limiter=None)

    assert scan["security_max_severity"] == "HIGH"
    assert scan["security_direct_deps_max_severity"] == "CRITICAL"


def test_dep_pass_severity_none_when_no_dep_advisory_carries_a_label(monkeypatch):
    """Vulnerable deps but every advisory is label-less (common for
    PYSEC-sourced entries) -> max_severity is None, not a fabricated floor,
    while vuln_count still reflects the real advisories."""
    _install(
        monkeypatch,
        deps={"rootpkg": {"dep-a": "^1"}},
        osv={
            "rootpkg": {"vulns": []},
            "dep-a": _osv_vulns(("PYSEC-1", None), ("PYSEC-2", None)),
        },
    )

    scan = fetch_mcp_security.fetch_osv_scan_with_deps("rootpkg", "npm", limiter=None)

    assert scan["security_direct_deps_vuln_count"] == 2
    assert scan["security_direct_deps_max_severity"] is None
    assert scan["security_direct_deps_vuln_ids"] == ["PYSEC-1", "PYSEC-2"]


def test_dep_vuln_ids_are_deduped_and_capped(monkeypatch):
    """Two deps sharing an advisory id -> it appears once. The list is
    capped at MAX_DEP_VULN_IDS."""
    many = _osv_vulns(*[(f"GHSA-{i:03d}", "LOW") for i in range(80)])
    _install(
        monkeypatch,
        deps={"rootpkg": {"dep-a": "^1", "dep-b": "^2"}},
        osv={
            "rootpkg": {"vulns": []},
            "dep-a": _osv_vulns(("GHSA-shared", "HIGH"), ("GHSA-a-only", "LOW")),
            "dep-b": {"vulns": [*many["vulns"], {"id": "GHSA-shared",
                                                 "database_specific": {"severity": "HIGH"}}]},
        },
    )

    scan = fetch_mcp_security.fetch_osv_scan_with_deps("rootpkg", "npm", limiter=None)

    ids = scan["security_direct_deps_vuln_ids"]
    assert len(ids) == fetch_mcp_security.MAX_DEP_VULN_IDS
    assert ids.count("GHSA-shared") == 1
    assert ids[:2] == ["GHSA-shared", "GHSA-a-only"]


def test_dep_pass_written_unconditionally_when_no_deps(monkeypatch):
    """A package with zero declared dependencies still gets the dep fields
    (as 0 / [] / None) -- 'checked, nothing downstream', not a skip."""
    _install(monkeypatch, deps={"rootpkg": {}}, osv={"rootpkg": {"vulns": []}})

    scan = fetch_mcp_security.fetch_osv_scan_with_deps("rootpkg", "npm", limiter=None)

    assert scan["security_direct_deps_scanned"] == 0
    assert scan["security_direct_deps_vuln_count"] == 0
    assert scan["security_direct_deps_with_vulns"] == []
    assert scan["security_direct_deps_max_severity"] is None
    assert scan["security_direct_deps_vuln_ids"] == []
