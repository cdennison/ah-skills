#!/usr/bin/env python3
"""Snapshot of the MCP discovery pipeline's current state -- registry
coverage, per-source breakdown, deployment/registry-type mix, readme
coverage, and the CSV export. Same spirit and shape as ../stats.py for the
skills pipeline, adapted for this one's schema (mcp_registry.py's
sources[]-per-row model instead of registry.py's).

No qdrant section: PROPOSED_PIPELINE.md's instruction for this pass is
"download only, no indexing yet," so there's nothing indexed to diff
against -- that section gets added once indexing is wired up.

Safe to run at any time, including while pull_seed_repo.py /
pull_official_registry.py / pull_glama.py / download_readmes.py are still
running in the background -- it only reads whatever's on disk right now.

Usage:
    python mcp_stats.py
"""

import csv
import json
from collections import Counter

import mcp_registry
from export_mcp_csv import OUTPUT_PATH as CSV_PATH

csv.field_size_limit(10_000_000)  # readmes can be large; same rationale as ../stats.py


def infer_deployment(entry: dict) -> str:
    """Deployment mode isn't stored under one uniform key across sources --
    pull_seed_repo.py's scan_entry() computes it directly (local/remote/
    hybrid), pull_official_registry.py only stores has_installable_package/
    has_remote, and pull_glama.py stores it inside Glama's own attributes[]
    list (hosting:local-only / hosting:remote-capable / hosting:hybrid).
    Roll all three up into one answer for reporting, preferring the most
    direct signal first."""
    sources = entry.get("sources", [])
    for s in sources:
        if s.get("deployment"):
            return s["deployment"]
    for s in sources:
        if s.get("type") == "glama":
            attrs = s.get("attributes") or []
            if "hosting:hybrid" in attrs:
                return "hybrid"
            if "hosting:remote-capable" in attrs:
                return "remote"
            if "hosting:local-only" in attrs:
                return "local"
    for s in sources:
        if s.get("type") == "official_registry":
            has_pkg, has_remote = s.get("has_installable_package"), s.get("has_remote")
            if has_pkg and has_remote:
                return "hybrid"
            if has_pkg:
                return "local"
            if has_remote:
                return "remote"
    return "(unknown)"


def first_value(entry: dict, field: str):
    for s in entry.get("sources", []):
        if s.get(field):
            return s[field]
    return None


def print_registry_overview(registry: list[dict]) -> None:
    print("--- registry (mcp-repo-seeds/registry.json) ---")
    print(f"Total unique servers: {len(registry):,}")
    print()

    source_counts: Counter = Counter()
    for r in registry:
        for s in r.get("sources", []):
            source_counts[s["type"]] += 1
    print("By source channel (a server can count toward more than one):")
    for source, count in source_counts.most_common():
        print(f"  {source:<22} {count:,}")
    print()

    multi_source = sum(1 for r in registry if len(r.get("sources", [])) > 1)
    pct = 100 * multi_source / len(registry) if registry else 0
    print(f"Corroborated by >1 source: {multi_source:,} ({pct:.1f}%)")

    with_errors = sum(1 for r in registry if r.get("errors"))
    error_status = sum(1 for r in registry if r.get("status") == "error")
    print(f"Rows with >=1 recorded error: {with_errors:,}")
    print(f"Rows with status=error:       {error_status:,}")
    print()


def print_seed_scan_progress(registry: list[dict]) -> None:
    total = scanned = no_manifest = 0
    for r in registry:
        d = mcp_registry.get_source(r, "awesome-mcp-servers")
        if not d:
            continue
        total += 1
        if d.get("scanned_at"):
            scanned += 1
            if not d.get("manifest_source"):
                no_manifest += 1

    print("--- seed repo scan (awesome-mcp-servers, punkpeye/awesome-mcp-servers) ---")
    print(f"Entries tracked: {total:,}")
    if total:
        print(f"Scanned:         {scanned:,} ({100 * scanned / total:.1f}%)")
    print(f"  of which had no server.json/package.json (dead repo / unrecognized shape): {no_manifest:,}")
    print(f"Pending scan:    {total - scanned:,}")
    print()


def print_classification(registry: list[dict]) -> None:
    category_counts: Counter = Counter()
    source_counts: Counter = Counter()
    for r in registry:
        category = r.get("mcp_category")
        category_counts[category or "(not yet classified)"] += 1
        if category:
            source_counts[r.get("mcp_category_source") or "(unknown)"] += 1

    print("--- mcp_category (classify_mcp_registry.py) ---")
    for k, v in category_counts.most_common():
        print(f"  {k:<22} {v:,}")
    print()
    print("By classification basis:")
    for k, v in source_counts.most_common():
        print(f"  {k:<22} {v:,}")
    print()


def print_deployment_and_registry_type(registry: list[dict]) -> None:
    deployment_counts: Counter = Counter()
    registry_type_counts: Counter = Counter()
    for r in registry:
        deployment_counts[infer_deployment(r)] += 1
        registry_type_counts[first_value(r, "registry_type") or "(none/unrecognized)"] += 1

    print("--- deployment mode (best available signal across sources) ---")
    for k, v in deployment_counts.most_common():
        print(f"  {k:<22} {v:,}")
    print()
    print("--- package registry type ---")
    for k, v in registry_type_counts.most_common():
        print(f"  {k:<22} {v:,}")
    print()


def print_readme_coverage(registry: list[dict]) -> None:
    resolvable = [r for r in registry if r.get("repo_url") and mcp_registry.parse_github_repo_url(r["repo_url"])]
    with_readme = sum(1 for r in resolvable if r.get("readme_path"))
    no_repo = len(registry) - len(resolvable)
    on_disk = len(list(mcp_registry.README_DIR.glob("*.md"))) if mcp_registry.README_DIR.exists() else 0

    print("--- readme coverage ---")
    print(f"Rows with a resolvable GitHub repo_url: {len(resolvable):,}")
    if resolvable:
        print(f"  of which have a readme fetched:       {with_readme:,} ({100 * with_readme / len(resolvable):.1f}%)")
    print(f"Rows with no resolvable repo (can't fetch a readme): {no_repo:,}")
    print(f"Readme files on disk ({mcp_registry.README_DIR}): {on_disk:,}")
    print()


def print_ranking_coverage(registry: list[dict]) -> None:
    """fetch_mcp_rankings.py backfills these two fields onto the row
    directly (not per-source) -- see mcp_registry.py's set_stars/
    set_downloads. Reported here the same way readme coverage is, so a
    partial overnight run's progress is visible without waiting for it to
    finish."""
    with_stars = [r for r in registry if r.get("stars") is not None]
    with_downloads = [r for r in registry if r.get("weekly_downloads") is not None]
    npm_rows = sum(1 for r in registry if first_value(r, "registry_type") == "npm")

    print("--- ranking data (fetch_mcp_rankings.py) ---")
    pct_stars = 100 * len(with_stars) / len(registry) if registry else 0
    print(f"Rows with a GitHub star count: {len(with_stars):,}/{len(registry):,} ({pct_stars:.1f}%)")
    if with_stars:
        top = sorted(with_stars, key=lambda r: -r["stars"])[:5]
        print("  top 5 by stars: " + ", ".join(f"{r.get('name') or r['id']} ({r['stars']:,})" for r in top))

    pct_dl = 100 * len(with_downloads) / npm_rows if npm_rows else 0
    print(f"npm rows with a weekly-download count: {len(with_downloads):,}/{npm_rows:,} ({pct_dl:.1f}% of npm rows)")
    if with_downloads:
        top = sorted(with_downloads, key=lambda r: -r["weekly_downloads"])[:5]
        print(
            "  top 5 by weekly downloads: "
            + ", ".join(f"{r.get('name') or r['id']} ({r['weekly_downloads']:,})" for r in top)
        )

    with_npm_score = [r for r in registry if r.get("npm_score_final") is not None]
    with_dependents = [r for r in registry if r.get("npm_dependents") is not None]
    pct_score = 100 * len(with_npm_score) / npm_rows if npm_rows else 0
    print(
        f"npm rows with a quality/popularity/maintenance score: {len(with_npm_score):,}/{npm_rows:,} "
        f"({pct_score:.1f}% -- the rest fell back to the plain downloads-point endpoint, "
        f"which has no score/dependents data; see fetch_mcp_rankings.py)"
    )
    print(f"npm rows with a dependents count: {len(with_dependents):,}/{npm_rows:,}")
    if with_dependents:
        top = sorted(with_dependents, key=lambda r: -r["npm_dependents"])[:5]
        print("  top 5 by dependents: " + ", ".join(f"{r.get('name') or r['id']} ({r['npm_dependents']:,})" for r in top))
    print()


def print_raw_dumps() -> None:
    print("--- raw source dumps (mcp-search-raw/) ---")
    for name in ("official_registry.json", "glama.json"):
        path = mcp_registry.RAW_DIR / name
        if path.exists():
            n = len(json.loads(path.read_text()))
            print(f"  {name:<24} {n:,} entries ({path.stat().st_size / 1_000_000:.1f} MB)")
        else:
            print(f"  {name:<24} not found")
    seed_readme = mcp_registry.REPO_SEEDS_DIR / "awesome-mcp-servers" / "README.md"
    print(f"  vendored seed README:    {'present' if seed_readme.exists() else 'missing'} ({seed_readme})")
    print()


def print_csv_stats() -> None:
    print("--- mcp_servers_export.csv ---")
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="", encoding="utf-8") as f:
            row_count = sum(1 for _ in csv.reader(f)) - 1  # minus header
        print(f"Rows: {row_count:,}")
        print(f"File: {CSV_PATH}")
    else:
        print(f"Not found: {CSV_PATH} (run export_mcp_csv.py)")


def main() -> None:
    registry = mcp_registry.load_registry()
    print_registry_overview(registry)
    print_seed_scan_progress(registry)
    print_classification(registry)
    print_deployment_and_registry_type(registry)
    print_readme_coverage(registry)
    print_ranking_coverage(registry)
    print_raw_dumps()
    print_csv_stats()


if __name__ == "__main__":
    main()
