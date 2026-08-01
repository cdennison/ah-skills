#!/usr/bin/env python3
"""Search GitHub for repos matching a query and emit a ranked markdown table.

Uses the GitHub Search API (https://api.github.com/search/repositories),
sorted by stars (or best-match relevance), formatted similarly to the
per-topic source tables in EvanLi/Github-Ranking:
https://github.com/EvanLi/Github-Ranking/blob/master/source/

Usage:
    ./search_github.py "agent skills"
    ./search_github.py "agent skills" --sort best-match --top 20
    ./search_github.py "agent skills" --sort stars --top 50 --out results.md
    ./search_github.py "agents skills" --exact --format json --out repo-seeds/github_search_results.json

--exact drops results whose name+description don't contain both "agent" and
"skill" as substrings (case-insensitive) -- best-match ranking already surfaces
relevant repos well, but a stars sort pulls in large unrelated projects (e.g.
general agent platforms) that just mention the words in passing; --exact
filters those out regardless of sort order.

This script only writes a review queue (the --format json output) -- it
never touches repo-seeds/registry.json (the pipeline's single source of
truth) directly. After a human reviews the JSON output, feed the approved
repos into the registry with:

    ./registry.py add-search repo-seeds/github_search_results.json \\
        --approve owner/repo --approve owner2/repo2
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
API_URL = "https://api.github.com/search/repositories"
RATE_LIMIT_URL = "https://api.github.com/rate_limit"

MIN_DELAY_SECONDS = 5  # floor pause between requests even when rate limit is healthy
RATE_LIMIT_SAFETY_MARGIN = 5  # stop and wait when this few requests remain

# The search endpoint has its own, much stricter quota than core
# (30/min authenticated, 10/min unauthenticated) -- rate_limit exposes it
# separately under resources.search, so pace off of that instead of core.
RATE_LIMIT_RESOURCE = "search"


def load_github_pat():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "GITHUB_PAT":
                return value.strip().strip('"').strip("'")
    return os.environ.get("GITHUB_PAT")


def _auth_header(req, token):
    if token:
        req.add_header("Authorization", f"Bearer {token}")


def check_rate_limit(token):
    """Query GitHub's /rate_limit endpoint and return the pacing delay (seconds)
    to use before the next search request, mirroring clone_repos.py's approach:
    spread requests across the remaining quota/window, sleep out a hit window."""
    req = urllib.request.Request(RATE_LIMIT_URL)
    req.add_header("Accept", "application/vnd.github+json")
    _auth_header(req, token)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[warn] could not check rate limit ({e}); using default delay", file=sys.stderr)
        return MIN_DELAY_SECONDS

    bucket = data["resources"][RATE_LIMIT_RESOURCE]
    remaining = bucket["remaining"]
    reset_at = bucket["reset"]
    now = time.time()
    seconds_to_reset = max(reset_at - now, 1)

    if remaining <= RATE_LIMIT_SAFETY_MARGIN:
        wait = seconds_to_reset + 5
        print(f"[rate-limit] only {remaining} search requests left, resets in {seconds_to_reset:.0f}s -- sleeping {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
        return MIN_DELAY_SECONDS

    pace = seconds_to_reset / remaining
    return max(MIN_DELAY_SECONDS, pace)


def fetch_page(query, sort, page, per_page, token):
    params = {
        "q": query,
        "per_page": per_page,
        "page": page,
    }
    if sort != "best-match":
        params["sort"] = sort
        params["order"] = "desc"
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "ah-skills-search-demo")
    _auth_header(req, token)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"GitHub API error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


MAX_PAGES = 10  # GitHub search caps at 1000 results total; don't loop forever hunting for --exact matches


def _is_exact_match(repo):
    haystack = f"{repo['full_name']} {repo.get('description') or ''}".lower()
    return "agent" in haystack and "skill" in haystack


def search(query, sort, top, token, exact=False):
    per_page = 100 if exact else min(top, 100)
    matched = []
    page = 1
    while len(matched) < top and page <= MAX_PAGES:
        delay = check_rate_limit(token)
        if page > 1:
            print(f"[pace] sleeping {delay:.1f}s before next page", file=sys.stderr)
            time.sleep(delay)
        data = fetch_page(query, sort, page, per_page, token)
        batch = data.get("items", [])
        if not batch:
            break
        matched.extend(r for r in batch if not exact or _is_exact_match(r))
        page += 1
        if len(batch) < per_page:
            break
    return matched[:top]


def to_markdown(items, query, sort):
    lines = [
        f"# GitHub search: \"{query}\" (sort: {sort})",
        "",
        "| Rank | Repository | Stars | Forks | Language | Description |",
        "|---|---|---|---|---|---|",
    ]
    for i, repo in enumerate(items, 1):
        name = repo["full_name"]
        url = repo["html_url"]
        stars = repo["stargazers_count"]
        forks = repo["forks_count"]
        lang = repo.get("language") or "-"
        desc = (repo.get("description") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i} | [{name}]({url}) | {stars} | {forks} | {lang} | {desc} |")
    return "\n".join(lines) + "\n"


def to_json_records(items, query, sort, exact):
    """Shape matches what clone_repos.py needs (owner, repo, url) plus enough
    metadata for human review before anything gets added to MANUAL_REPOS.md."""
    records = []
    for i, repo in enumerate(items, 1):
        owner, _, name = repo["full_name"].partition("/")
        records.append(
            {
                "rank": i,
                "owner": owner,
                "repo": name,
                "full_name": repo["full_name"],
                "url": repo["html_url"],
                "stars": repo["stargazers_count"],
                "forks": repo["forks_count"],
                "language": repo.get("language"),
                "description": repo.get("description"),
                "reviewed": False,
                "approved": None,
            }
        )
    return {
        "query": query,
        "sort": sort,
        "exact": exact,
        "count": len(records),
        "results": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", help='Search phrase, e.g. "agent skills"')
    parser.add_argument("--sort", choices=["best-match", "stars", "forks", "updated"], default="best-match")
    parser.add_argument("--top", type=int, default=25, help="Number of repos to return (default 25)")
    parser.add_argument("--exact", action="store_true", help='Require "agent" and "skill" to both appear in name+description')
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--out", help="Write output to this file instead of stdout")
    args = parser.parse_args()

    token = load_github_pat()
    if not token:
        print("Warning: no GITHUB_PAT found (.env or env var) — using unauthenticated, low rate limit.", file=sys.stderr)

    items = search(args.query, args.sort, args.top, token, exact=args.exact)

    if args.format == "json":
        output = json.dumps(to_json_records(items, args.query, args.sort, args.exact), indent=2)
    else:
        output = to_markdown(items, args.query, args.sort)

    if args.out:
        Path(args.out).write_text(output + ("\n" if args.format == "json" else ""))
        print(f"Wrote {len(items)} results to {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
