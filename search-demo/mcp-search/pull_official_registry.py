#!/usr/bin/env python3
"""Step 2a of the MCP pipeline (PROPOSED_PIPELINE.md source 0) -- the
highest-priority structured source: registry.modelcontextprotocol.io is a
live, first-party, paginated API whose entries are already shaped like
server.json (name, description, packages[]/remotes[], env vars).

Paginates GET .../v0/servers?limit=100&cursor=...&version=latest --
`version=latest` is load-bearing: confirmed live that without it, the same
server name reappears once per historical published version (e.g. 3 rows
for one server across versions 1.0.0/1.0.1/2.0.0), which would silently
inflate the dataset with stale duplicates instead of one row per server.

Capped at --limit total entries (default 10000, per this pipeline's
instruction to grab up to 10k from each of the two structured-API sources).
Writes the full raw pull to mcp-search-raw/official_registry.json (for
re-processing without re-fetching) and upserts each into
mcp-repo-seeds/registry.json. Rate limited via shared.http.default_limiter().

Usage:
    python pull_official_registry.py
    python pull_official_registry.py --limit 200   # testing
"""

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from shared.http import default_limiter, get_json

BASE_URL = "https://registry.modelcontextprotocol.io/v0/servers"
PAGE_SIZE = 100
RAW_OUTPUT = mcp_registry.RAW_DIR / "official_registry.json"
SAVE_EVERY = 500


def fetch_all(limiter, limit: int) -> list[dict]:
    servers: list[dict] = []
    cursor = None
    while len(servers) < limit:
        params = {"limit": min(PAGE_SIZE, limit - len(servers)), "version": "latest"}
        if cursor:
            params["cursor"] = cursor
        url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
        page = get_json(url, limiter)
        page_servers = page.get("servers", [])
        if not page_servers:
            break
        servers.extend(page_servers)
        print(f"  pulled {len(servers)} so far...")
        cursor = (page.get("metadata") or {}).get("nextCursor")
        if not cursor:
            break
    return servers


def upsert_entry(registry: list[dict], item: dict, index: dict[str, dict]) -> dict:
    server = item.get("server", {})
    meta = (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official", {})
    name = server.get("name")
    repo_url = (server.get("repository") or {}).get("url")
    packages = server.get("packages") or []
    remotes = server.get("remotes") or []
    pkg = packages[0] if packages else {}

    return mcp_registry.upsert(
        registry,
        {
            "repo_url": repo_url,
            "source": "official_registry",
            "source_key": name,
            "name": name,
            "description": server.get("description"),
            "version": server.get("version"),
            "status_upstream": meta.get("status"),
            "published_at": meta.get("publishedAt"),
            "updated_at": meta.get("updatedAt"),
            "registry_type": pkg.get("registryType") if packages else None,
            "package_identifier": pkg.get("identifier") if packages else None,
            "has_installable_package": bool(packages),
            "has_remote": bool(remotes),
        },
        index=index,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10000, help="Max entries to pull (default 10000)")
    args = parser.parse_args()

    limiter = default_limiter()
    print(f"pulling up to {args.limit} entries from {BASE_URL} (version=latest)")
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
            name = (item.get("server") or {}).get("name") or f"entry_{i}"
            print(f"[warn] failed to process {name}: {e!r}", file=sys.stderr)
            failed += 1
        if i % SAVE_EVERY == 0:
            mcp_registry.save_registry(registry)
    mcp_registry.save_registry(registry)
    print(f"\ndone: {ok} upserted, {failed} failed, {len(registry)} total rows in registry")


if __name__ == "__main__":
    main()
