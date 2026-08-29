#!/usr/bin/env python3
"""Flatten mcp-repo-seeds/registry.json into ../mcp_servers_export.csv for
spreadsheet review -- one row per unique server (see mcp_registry.py's
dedup-by-GitHub-repo identity), covering whatever pull_official_registry.py
+ pull_glama.py + pull_seed_repo.py + download_readmes.py have gathered so
far. This is the "download, then confirm" checkpoint requested before any
qdrant indexing happens -- deliberately does not touch qdrant.

Usage:
    python export_mcp_csv.py
"""

import csv
import json
from pathlib import Path

import mcp_registry

OUTPUT_PATH = Path(__file__).parent.parent / "mcp_servers_export.csv"

FIELDS = [
    "id",
    "name",
    "description",
    "glama_description",
    "readme_description",
    "repo_url",
    "status",
    "mcp_category",
    "mcp_category_source",
    "sources",
    "source_count",
    "registry_type",
    "package_identifier",
    "package_url",
    "deployment",
    "transport",
    "has_installable_package",
    "has_remote",
    "attributes",
    "license",
    "env_vars_json",
    "env_vars_schema_json",
    "stars",
    "stars_updated",
    "weekly_downloads",
    "monthly_downloads",
    "npm_dependents",
    "npm_score_final",
    "npm_score_quality",
    "npm_score_popularity",
    "npm_score_maintenance",
    "downloads_source",
    "downloads_updated",
    "language",
    "security_vuln_count",
    "security_vuln_ids",
    "security_max_severity",
    "security_direct_deps_scanned",
    "security_direct_deps_vuln_count",
    "security_direct_deps_with_vulns",
    "security_source",
    "security_updated",
    "readme_path",
    "readme_source",
    "readme_fetched",
    "error_count",
    "last_error",
    "added",
]

# Fields that live inside a source descriptor (varies by which source(s)
# contributed it) rather than on the row itself -- pulled from whichever
# descriptor has a value, first source added wins ties.
DESCRIPTOR_FIELDS = (
    "registry_type",
    "package_identifier",
    "package_url",
    "deployment",
    "transport",
    "has_installable_package",
    "has_remote",
    "attributes",
    "license",
    "env_vars_json",
    "env_vars_schema_json",
)


def first_descriptor_value(entry: dict, field: str):
    for s in entry.get("sources", []):
        if s.get(field) not in (None, "", [], {}):
            return s[field]
    return None


def _readme_text(entry: dict) -> str:
    readme_path = entry.get("readme_path")
    if not readme_path:
        return ""
    full_path = mcp_registry.MCP_DIR.parent / readme_path
    if not full_path.exists():
        return ""
    return full_path.read_text(errors="ignore")


def to_row(entry: dict) -> dict:
    sources = entry.get("sources", [])
    errors = entry.get("errors", [])
    glama_source = next((s for s in sources if s["type"] == "glama"), None)

    row = {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "description": entry.get("description"),
        "glama_description": glama_source.get("description") if glama_source else None,
        "readme_description": mcp_registry.extract_readme_description(_readme_text(entry)),
        "repo_url": entry.get("repo_url"),
        "status": entry.get("status"),
        "mcp_category": entry.get("mcp_category"),
        "mcp_category_source": entry.get("mcp_category_source"),
        "sources": "+".join(s["type"] for s in sources),
        "source_count": len(sources),
        "stars": entry.get("stars"),
        "stars_updated": entry.get("stars_updated"),
        "weekly_downloads": entry.get("weekly_downloads"),
        "monthly_downloads": entry.get("monthly_downloads"),
        "npm_dependents": entry.get("npm_dependents"),
        "npm_score_final": entry.get("npm_score_final"),
        "npm_score_quality": entry.get("npm_score_quality"),
        "npm_score_popularity": entry.get("npm_score_popularity"),
        "npm_score_maintenance": entry.get("npm_score_maintenance"),
        "downloads_source": entry.get("downloads_source"),
        "downloads_updated": entry.get("downloads_updated"),
        "language": entry.get("language"),
        "security_vuln_count": entry.get("security_vuln_count"),
        "security_vuln_ids": json.dumps(entry.get("security_vuln_ids"), ensure_ascii=False)
        if entry.get("security_vuln_ids") is not None else None,
        "security_max_severity": entry.get("security_max_severity"),
        "security_direct_deps_scanned": entry.get("security_direct_deps_scanned"),
        "security_direct_deps_vuln_count": entry.get("security_direct_deps_vuln_count"),
        "security_direct_deps_with_vulns": json.dumps(entry.get("security_direct_deps_with_vulns"), ensure_ascii=False)
        if entry.get("security_direct_deps_with_vulns") is not None else None,
        "security_source": entry.get("security_source"),
        "security_updated": entry.get("security_updated"),
        "readme_path": entry.get("readme_path"),
        "readme_source": entry.get("readme_source"),
        "readme_fetched": entry.get("readme_fetched"),
        "error_count": len(errors),
        "last_error": errors[-1]["message"] if errors else "",
        "added": entry.get("added"),
    }
    for field in DESCRIPTOR_FIELDS:
        value = first_descriptor_value(entry, field)
        row[field] = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value

    return row


def main():
    registry = mcp_registry.load_registry()

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for entry in registry:
            writer.writerow(to_row(entry))

    print(f"wrote {len(registry)} rows to {OUTPUT_PATH}")

    by_source: dict[str, int] = {}
    for entry in registry:
        for s in entry.get("sources", []):
            by_source[s["type"]] = by_source.get(s["type"], 0) + 1
    for source, count in sorted(by_source.items()):
        print(f"  {source}: {count}")

    with_readme = sum(1 for e in registry if e.get("readme_path"))
    with_errors = sum(1 for e in registry if e.get("errors"))
    multi_source = sum(1 for e in registry if len(e.get("sources", [])) > 1)
    print(f"{with_readme}/{len(registry)} have a readme")
    print(f"{with_errors}/{len(registry)} have at least one recorded error")
    print(f"{multi_source}/{len(registry)} corroborated by more than one source")


if __name__ == "__main__":
    main()
