#!/usr/bin/env python3
"""Step 2b of the MCP pipeline (PROPOSED_PIPELINE.md source 1) -- Glama.ai's
public, cursor-paginated, no-auth MCP directory API
(glama.ai/api/mcp/v1/servers). No sort/rank parameter exists (confirmed
against the published OpenAPI spec, and by testing sort=/sortBy=/attributes=
directly -- all silently ignored), so exhaustive pagination is the only
mode; default order is createdAt descending.

Per PROPOSED_PIPELINE.md's source-1 notes: Glama's description/attributes/
environmentVariablesJsonSchema are trusted and preferred over self-derived
versions (mcp_registry.upsert already special-cases source="glama" to
overwrite `description` on match) -- but its *displayed* download/star
numbers and even its `repository.url` are explicitly NOT to be trusted
blindly (confirmed unreliable for at least one entry during the
investigation this pipeline is built from). This script stores
repository.url as-is for identity/dedup purposes only; ranking data must
come from GitHub/npm directly in a later step, never from here.

Capped at --limit total entries (default 10000). Writes the full raw pull
to mcp-search-raw/glama.json and upserts each into
mcp-repo-seeds/registry.json. Rate limited via shared.http.default_limiter().

Usage:
    python pull_glama.py
    python pull_glama.py --limit 200   # testing
"""

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from shared.http import default_limiter, get_json

BASE_URL = "https://glama.ai/api/mcp/v1/servers"
PAGE_SIZE = 100
RAW_OUTPUT = mcp_registry.RAW_DIR / "glama.json"
SAVE_EVERY = 500


def fetch_all(limiter, limit: int) -> list[dict]:
    servers: list[dict] = []
    after = None
    while len(servers) < limit:
        params = {"first": min(PAGE_SIZE, limit - len(servers))}
        if after:
            params["after"] = after
        url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
        page = get_json(url, limiter)
        page_servers = page.get("servers", [])
        if not page_servers:
            break
        servers.extend(page_servers)
        print(f"  pulled {len(servers)} so far...")
        page_info = page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
    return servers


def upsert_entry(registry: list[dict], item: dict, index: dict[str, dict]) -> dict:
    repo_url = (item.get("repository") or {}).get("url")
    env_schema = item.get("environmentVariablesJsonSchema")
    return mcp_registry.upsert(
        registry,
        {
            "repo_url": repo_url,
            "source": "glama",
            "source_key": item.get("slug") or item.get("id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "glama_id": item.get("id"),
            "glama_url": item.get("url"),
            "attributes": item.get("attributes"),
            "env_vars_schema_json": json.dumps(env_schema) if env_schema else None,
            "license": (item.get("spdxLicense") or {}).get("name"),
            "tools": item.get("tools"),
        },
        index=index,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10000, help="Max entries to pull (default 10000)")
    args = parser.parse_args()

    limiter = default_limiter()
    print(f"pulling up to {args.limit} entries from {BASE_URL}")
    servers = fetch_all(limiter, args.limit)
    print(f"pulled {len(servers)} raw entries")

    mcp_registry.RAW_DIR.mkdir(parents=True, exist_ok=True)
    RAW_OUTPUT.write_text(json.dumps(servers, indent=2))
    print(f"wrote raw dump to {RAW_OUTPUT}")

    registry = mcp_registry.load_registry()
    index = mcp_registry.build_index(registry)
    ok = failed = 0
    for i, item in enumerate(servers, start=1):
        try:
            upsert_entry(registry, item, index)
            ok += 1
        except Exception as e:
            print(f"[warn] failed to process {item.get('id', f'entry_{i}')}: {e!r}", file=sys.stderr)
            failed += 1
        if i % SAVE_EVERY == 0:
            mcp_registry.save_registry(registry)
    mcp_registry.save_registry(registry)
    print(f"\ndone: {ok} upserted, {failed} failed, {len(registry)} total rows in registry")


if __name__ == "__main__":
    main()
