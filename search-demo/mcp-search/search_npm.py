#!/usr/bin/env python3
"""Search the public npm registry for packages by keyword (e.g. "mcp") and
print candidate MCP servers to console for review.

Uses the documented npm registry search endpoint:
https://api-docs.npmjs.com/#tag/Search -- GET /-/v1/search on
registry.npmjs.org (not api.npmjs.org, despite the docs subdomain name).

Usage:
    python search_npm.py mcp
    python search_npm.py mcp --size 50
"""

import argparse
import json
import urllib.parse
import urllib.request

SEARCH_URL = "https://registry.npmjs.org/-/v1/search"


def search_npm(text: str, size: int = 20, from_: int = 0) -> list[dict]:
    """Query the npm registry search endpoint and return the raw list of
    result objects (each with "package" and "score" keys)."""
    params = urllib.parse.urlencode({"text": text, "size": size, "from": from_})
    with urllib.request.urlopen(f"{SEARCH_URL}?{params}") as resp:
        data = json.load(resp)
    return data["objects"]


def summarize(objects: list[dict]) -> list[dict]:
    """Reduce raw search results to the fields useful for MCP server
    candidate review -- name, description, repo, npm page, popularity."""
    summaries = []
    for obj in objects:
        pkg = obj["package"]
        links = pkg.get("links", {})
        summaries.append(
            {
                "name": pkg["name"],
                "description": pkg.get("description"),
                "version": pkg.get("version"),
                "repo_url": links.get("repository"),
                "package_url": links.get("npm"),
                "monthly_downloads": obj.get("downloads", {}).get("monthly"),
                "search_score": obj.get("searchScore"),
            }
        )
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help='Search query, e.g. "mcp"')
    parser.add_argument("--size", type=int, default=20, help="Number of results (max 250 per npm)")
    parser.add_argument("--from", dest="from_", type=int, default=0, help="Pagination offset")
    args = parser.parse_args()

    objects = search_npm(args.text, size=args.size, from_=args.from_)
    print(json.dumps(summarize(objects), indent=2))


if __name__ == "__main__":
    main()
