#!/usr/bin/env python3
"""Fetch the top N npm search results for a query (default "mcp") and, for
each one, pull the full package detail (readme + metadata) from the npm
registry -- rate limited to stay well under 1 req/s and 10 req/min.

Two endpoints, per https://api-docs.npmjs.com/:
  1. GET registry.npmjs.org/-/v1/search?text=...&size=...   (one call, gets
     up to 250 hits at once -- this is what search_npm.py already wraps)
  2. GET registry.npmjs.org/<package>                        (one call per
     package -- this is the slow, rate-limited part; readme lives here)

Output is written to npm_mcp_candidates.json next to this script (100
full readmes is too much to dump to console); a compact progress line
prints per package as it's fetched.

Usage:
    python fetch_npm_mcp_candidates.py
    python fetch_npm_mcp_candidates.py --query mcp --limit 100
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search_npm import search_npm, summarize
from shared.rate_limit import sleep_if_more

OUTPUT_PATH = Path(__file__).parent / "npm_mcp_candidates.json"

# Spacing between package-detail requests. 6.5s -> ~9.2 req/min, safely
# under both "less than 1 req/s" and "less than 10 per min".
REQUEST_INTERVAL_SECONDS = 6.5


def fetch_package_detail(name: str) -> dict | None:
    """GET the full package doc from the npm registry (readme, license,
    maintainers, dependencies, engines, publish time, etc). Returns None on
    a 404 (package removed/unpublished between search and fetch)."""
    encoded = urllib.parse.quote(name, safe="")
    url = f"https://registry.npmjs.org/{encoded}"
    try:
        with urllib.request.urlopen(url) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def summarize_detail(name: str, doc: dict | None) -> dict:
    if doc is None:
        return {"name": name, "error": "not found (404)"}

    latest = doc.get("dist-tags", {}).get("latest")
    latest_version = (doc.get("versions") or {}).get(latest, {})

    return {
        "name": doc.get("name", name),
        "description": doc.get("description"),
        "version": latest,
        "license": doc.get("license"),
        "homepage": doc.get("homepage"),
        "repository": doc.get("repository"),
        "author": doc.get("author"),
        "maintainers": doc.get("maintainers"),
        "keywords": latest_version.get("keywords"),
        "engines": latest_version.get("engines"),
        "dependencies": latest_version.get("dependencies"),
        "has_mcp_sdk_dependency": "@modelcontextprotocol/sdk" in (latest_version.get("dependencies") or {}),
        "created": (doc.get("time") or {}).get("created"),
        "modified": (doc.get("time") or {}).get("modified"),
        "readme": doc.get("readme") or None,
        "readme_filename": doc.get("readmeFilename") or None,
    }


def fetch_top_candidates(query: str, limit: int, interval: float = REQUEST_INTERVAL_SECONDS) -> list[dict]:
    search_hits = summarize(search_npm(query, size=limit))
    print(f"search: {len(search_hits)} hits for {query!r}")

    results = []
    for i, hit in enumerate(search_hits, start=1):
        name = hit["name"]
        doc = fetch_package_detail(name)
        detail = summarize_detail(name, doc)
        detail["search_score"] = hit.get("search_score")
        detail["monthly_downloads"] = hit.get("monthly_downloads")
        results.append(detail)

        status = "ok" if doc is not None else "MISSING"
        print(f"[{i}/{len(search_hits)}] {name} ({status})")
        sleep_if_more(i, len(search_hits), interval)

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="mcp", help='Search query (default "mcp")')
    parser.add_argument("--limit", type=int, default=100, help="Number of top search hits to fetch (default 100)")
    parser.add_argument(
        "--interval",
        type=float,
        default=REQUEST_INTERVAL_SECONDS,
        help=f"Seconds between package-detail requests (default {REQUEST_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()

    results = fetch_top_candidates(args.query, args.limit, args.interval)

    OUTPUT_PATH.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {len(results)} entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
