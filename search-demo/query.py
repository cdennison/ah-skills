#!/usr/bin/env python3
"""CLI to semantically search indexed content -- thin wrapper around the
same search_skills()/search_mcp_servers() the Streamlit app and query
service use (app/search.py, app/mcp_search.py), so the CLI never drifts
from them on prefetch/fusion/filter behavior. --asset-type picks which
Qdrant collection to search: "skill" (agent_skills, default) or "mcp"
(mcp_servers).

Usage:
    uv run python query.py "excel spreadsheets" [-n 5]
    uv run python query.py "browser automation server" --asset-type mcp
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from mcp_search import search_mcp_servers  # noqa: E402
from search import search_skills  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Search indexed agent skills or MCP servers")
    parser.add_argument("query", help="natural language search query")
    parser.add_argument("-n", "--limit", type=int, default=5, help="number of results (default 5)")
    parser.add_argument(
        "--asset-type",
        choices=["skill", "mcp"],
        default="skill",
        help="which collection to search: 'skill' (agent_skills, default) or 'mcp' (mcp_servers)",
    )
    args = parser.parse_args()

    if args.asset_type == "mcp":
        for result in search_mcp_servers(args.query, limit=args.limit):
            score = f"{result.score:.3f}" if result.score is not None else "  n/a"
            print(f"{score}  {result.name}  [{result.mcp_category or 'unclassified'}]")
            if result.description:
                print(f"       {result.description}")
            if result.repo_url:
                print(f"       {result.repo_url}")
    else:
        for result in search_skills(args.query, limit=args.limit):
            score = f"{result.score:.3f}" if result.score is not None else "  n/a"
            print(f"{score}  {result.path}")
            if result.description:
                print(f"       {result.description}")


if __name__ == "__main__":
    main()
