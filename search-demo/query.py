#!/usr/bin/env python3
"""CLI to semantically search indexed SKILL.md files -- thin wrapper around
the same `search_skills()` the Streamlit app uses (app/search.py), so the
CLI and the UI never drift on prefetch/fusion/filter behavior.

Usage:
    uv run python query.py "excel spreadsheets" [-n 5]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from search import search_skills  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Search indexed agent skills")
    parser.add_argument("query", help="natural language search query")
    parser.add_argument("-n", "--limit", type=int, default=5, help="number of results (default 5)")
    args = parser.parse_args()

    for result in search_skills(args.query, limit=args.limit):
        score = f"{result.score:.3f}" if result.score is not None else "  n/a"
        print(f"{score}  {result.path}")
        if result.description:
            print(f"       {result.description}")


if __name__ == "__main__":
    main()
