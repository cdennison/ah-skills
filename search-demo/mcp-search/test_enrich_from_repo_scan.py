"""Unit coverage for the repo-scan -> registry.json merge step (E2E Test
Plan 03, Findings 1 & 2): scan_mcp extraction must actually land on
existing Glama-/official-registry-sourced rows, additively and
provenance-stamped, and one repo with no manifest must not kill the batch.

Hermetic -- no network, no real registry.json: a fake scan_entry stands in
for the GitHub fetch, and save_registry is stubbed out.
"""

import mcp_registry
import enrich_from_repo_scan
from export_mcp_csv import first_descriptor_value


SERVER_JSON_SCAN = {
    "name": "io.github.upstash/context7",
    "description": "Up-to-date code docs for any prompt",
    "registry_type": "npm",
    "package_identifier": "@upstash/context7-mcp",
    "package_url": "https://www.npmjs.com/package/@upstash/context7-mcp",
    "deployment": "hybrid",
    "transport": "stdio",
    "remote_urls": ["https://mcp.context7.com/mcp"],
    "has_installable_package": True,
    "source_file": "server.json",
}
PACKAGE_JSON_SCAN = {
    "name": "some-npm-mcp",
    "registry_type": "npm",
    "package_identifier": "some-npm-mcp",
    "package_url": "https://www.npmjs.com/package/some-npm-mcp",
    "transport": None,
    "source_file": "package.json",
}
PYPROJECT_SCAN = {
    "name": "daytona-mcp-interpreter",
    "registry_type": "pypi",
    "package_identifier": "daytona-mcp-interpreter",
    "package_url": "https://pypi.org/project/daytona-mcp-interpreter/",
    "deployment": "local",
    "has_installable_package": True,
    "console_scripts": ["daytona-interpreter"],
    "pyproject_dependencies": ["mcp", "pydantic", "httpx"],
    "source_file": "pyproject.toml",
}


def _row(entry_id, repo_url, **extra):
    return {
        "id": entry_id,
        "name": entry_id,
        "description": "",
        "repo_url": repo_url,
        "status": "active",
        "sources": [{"type": "glama", "added": "2026-08-01", "attributes": ["hosting:remote-capable"]}],
        "errors": [],
        **extra,
    }


def test_merge_repo_scan_is_additive_and_provenance_stamped():
    registry = [_row("github:upstash/context7", "https://github.com/upstash/context7")]
    index = mcp_registry.build_index(registry)

    mcp_registry.merge_repo_scan(registry, "github:upstash/context7", SERVER_JSON_SCAN, index=index)

    row = registry[0]
    desc = mcp_registry.get_source(row, "repo_scan")
    assert desc is not None
    assert desc["registry_type"] == "npm"
    assert desc["package_identifier"] == "@upstash/context7-mcp"
    assert desc["deployment"] == "hybrid"
    assert desc["transport"] == "stdio"
    assert desc["has_remote"] is True  # derived from remote_urls
    assert desc["manifest_source"] == "server.json"
    assert desc["scanned_at"]
    assert row["repo_scan_source"] == "scan_mcp"
    assert row["repo_scan_updated"]
    # the original glama source is untouched
    assert mcp_registry.get_source(row, "glama")["attributes"] == ["hosting:remote-capable"]
    # and first_descriptor_value (what index_qdrant.py/export_mcp_csv.py use)
    # now resolves the derived fields off the new descriptor
    assert first_descriptor_value(row, "deployment") == "hybrid"
    assert first_descriptor_value(row, "transport") == "stdio"


def test_merge_repo_scan_never_clobbers_existing_value_with_null():
    registry = [_row("github:x/y", "https://github.com/x/y")]
    index = mcp_registry.build_index(registry)

    mcp_registry.merge_repo_scan(registry, "github:x/y", PACKAGE_JSON_SCAN, index=index)
    desc = mcp_registry.get_source(registry[0], "repo_scan")
    assert "transport" not in desc  # None scan value never written

    # a later scan that DOES have a transport fills it, without wiping the id
    mcp_registry.merge_repo_scan(
        registry, "github:x/y", {**PACKAGE_JSON_SCAN, "transport": "stdio"}, index=index
    )
    desc = mcp_registry.get_source(registry[0], "repo_scan")
    assert desc["transport"] == "stdio"
    assert desc["package_identifier"] == "some-npm-mcp"


def test_merge_repo_scan_noops_for_unknown_id():
    registry = [_row("github:x/y", "https://github.com/x/y")]
    assert mcp_registry.merge_repo_scan(registry, "github:not/here", SERVER_JSON_SCAN) is None


def test_enrich_processes_mixed_manifest_list_without_raising(monkeypatch):
    """Finding 2(a) acceptance: a list with a server.json repo, a
    package.json repo, a bare-pyproject repo, AND a no-manifest repo runs to
    completion -- the bad one logs a skip, the rest merge."""
    registry = [
        _row("github:upstash/context7", "https://github.com/upstash/context7"),
        _row("github:acme/npm-server", "https://github.com/acme/npm-server"),
        _row("github:nibzard/daytona-mcp-interpreter", "https://github.com/nibzard/daytona-mcp-interpreter"),
        _row("github:dead/repo", "https://github.com/dead/repo"),
        _row("official:closed-remote-thing", None),  # no repo_url -> not eligible
    ]
    index = mcp_registry.build_index(registry)

    scans = {
        "upstash/context7": SERVER_JSON_SCAN,
        "acme/npm-server": PACKAGE_JSON_SCAN,
        "nibzard/daytona-mcp-interpreter": PYPROJECT_SCAN,
    }

    def fake_scan_entry(fetch, label):
        if label in scans:
            return scans[label]
        raise ValueError(f"no server.json, package.json, or pyproject.toml found in {label}")

    monkeypatch.setattr(enrich_from_repo_scan, "scan_entry", fake_scan_entry)
    monkeypatch.setattr(mcp_registry, "save_registry", lambda _registry: None)

    only_ids = {r["id"] for r in registry}
    enrich_from_repo_scan.enrich(
        registry, index, limiter=None,
        limit=None, random_sample=None, rescan=True, stale_days=0, only_ids=only_ids,
    )

    assert mcp_registry.get_source(registry[0], "repo_scan")["registry_type"] == "npm"
    assert mcp_registry.get_source(registry[1], "repo_scan")["registry_type"] == "npm"
    assert mcp_registry.get_source(registry[2], "repo_scan")["pyproject_dependencies"] == ["mcp", "pydantic", "httpx"]
    # the no-manifest repo: skipped, recorded as an error, still no repo_scan descriptor
    assert mcp_registry.get_source(registry[3], "repo_scan") is None
    assert registry[3]["errors"] and registry[3]["errors"][-1]["source"] == "repo_scan"
    # the repo-less row was never a candidate
    assert mcp_registry.get_source(registry[4], "repo_scan") is None


def test_enrich_ids_targeting_skips_non_requested_rows(monkeypatch):
    registry = [
        _row("github:a/one", "https://github.com/a/one"),
        _row("github:b/two", "https://github.com/b/two"),
    ]
    index = mcp_registry.build_index(registry)
    monkeypatch.setattr(enrich_from_repo_scan, "scan_entry", lambda fetch, label: {**PACKAGE_JSON_SCAN, "name": label})
    monkeypatch.setattr(mcp_registry, "save_registry", lambda _registry: None)

    enrich_from_repo_scan.enrich(
        registry, index, limiter=None,
        limit=None, random_sample=None, rescan=False, stale_days=30, only_ids={"github:a/one"},
    )

    assert mcp_registry.get_source(registry[0], "repo_scan") is not None
    assert mcp_registry.get_source(registry[1], "repo_scan") is None
