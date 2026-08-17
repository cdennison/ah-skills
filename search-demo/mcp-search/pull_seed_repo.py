#!/usr/bin/env python3
"""Step 1 of the MCP pipeline (PROPOSED_PIPELINE.md source 6): vendor
punkpeye/awesome-mcp-servers, parse every entry under "## Server
Implementations", and run each one through scan_mcp.py's extraction (no
clone -- raw.githubusercontent.com only) into mcp-repo-seeds/registry.json.

Reuses scan_mcp.py's scan_entry/RAW_GITHUB_URL directly (same extraction
priority: server.json, then package.json) rather than reimplementing it --
only the Fetcher passed in is new here (rate-limited via shared.http).

Rate limited via shared.http.github_limiter() (4000/hr, shared across every
GitHub host this pipeline hits, plus a 10/s burst guard) since every entry
costs 1-3 raw-content requests (server.json, package.json fallback,
README.md link-fallback). At ~3300 entries in the current seed list this is
a long run -- progress is saved to
registry.json incrementally (every SAVE_EVERY entries), and each attempt
(success or failure) is stamped `scanned_at` so a re-run by default only
retries entries that were never attempted, not the whole list (--rescan
forces a full re-attempt).

Usage:
    python pull_seed_repo.py                # full run, all entries
    python pull_seed_repo.py --limit 20      # first 20 (for testing)
    python pull_seed_repo.py --rescan        # re-attempt entries already scanned (incl. failures)
    python pull_seed_repo.py --vendor-only   # just refresh the vendored README + register names/urls, no scanning
"""

import argparse
import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from scan_mcp import RAW_GITHUB_URL, scan_entry
from shared.http import get_text_or_none, github_limiter

SEED_NAME = "awesome-mcp-servers"
UPSTREAM_REPO = "https://github.com/punkpeye/awesome-mcp-servers"
README_URL = "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/HEAD/README.md"
VENDORED_PATH = "mcp-repo-seeds/awesome-mcp-servers/README.md"
ENTRY_RE = re.compile(r"^- \[([^\]]+)\]\((https://github\.com/[^)\s]+)\)", re.MULTILINE)

SAVE_EVERY = 25


def fetch_and_vendor_readme(limiter) -> str:
    text = get_text_or_none(README_URL, limiter)
    if text is None:
        raise RuntimeError(f"seed README not found at {README_URL}")
    vendored = mcp_registry.REPO_SEEDS_DIR / "awesome-mcp-servers" / "README.md"
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text(text)
    mcp_registry.mark_seed_pulled(SEED_NAME, UPSTREAM_REPO, VENDORED_PATH)
    return text


def parse_server_entries(readme_text: str) -> list[tuple[str, str]]:
    """Only the '## Server Implementations' section -- the README's other
    sections (Clients, Frameworks, Tips and Tricks, ...) also contain
    github.com links but aren't MCP servers."""
    start = readme_text.find("## Server Implementations")
    if start == -1:
        raise ValueError("could not find '## Server Implementations' section in seed README")
    end = readme_text.find("\n## ", start + 1)
    section = readme_text[start : end if end != -1 else None]
    return ENTRY_RE.findall(section)


def rate_limited_github_fetcher(owner: str, repo: str, limiter):
    def fetch(path: str):
        url = RAW_GITHUB_URL.format(owner=owner, repo=repo, path=path)
        return get_text_or_none(url, limiter)

    return fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N entries (testing)")
    parser.add_argument("--rescan", action="store_true", help="Re-attempt entries already scanned, including prior failures")
    parser.add_argument("--vendor-only", action="store_true", help="Only vendor the README + register names/urls, skip per-repo scanning")
    args = parser.parse_args()

    limiter = github_limiter()

    print(f"fetching seed README from {README_URL}")
    text = fetch_and_vendor_readme(limiter)
    entries = parse_server_entries(text)
    if args.limit:
        entries = entries[: args.limit]
    print(f"parsed {len(entries)} entries from '## Server Implementations'")

    registry = mcp_registry.load_registry()
    index = mcp_registry.build_index(registry)

    # Register every entry's name+url up front, even before scanning -- so a
    # dead/gone repo is still tracked as a known candidate, not silently
    # dropped if scanning fails or is skipped.
    to_scan = []
    for name, url in entries:
        owner_repo = mcp_registry.parse_github_repo_url(url)
        if not owner_repo:
            continue
        owner, repo = owner_repo
        row = mcp_registry.upsert(
            registry,
            {
                "repo_url": url,
                "source": "awesome-mcp-servers",
                "source_key": f"{owner}/{repo}",
                "name": name,
                "list_entry_name": name,
            },
            index=index,
        )
        descriptor = mcp_registry.get_source(row, "awesome-mcp-servers")
        already_scanned = bool(descriptor and descriptor.get("scanned_at"))
        if args.rescan or not already_scanned:
            to_scan.append((row["id"], owner, repo, url, name))
    mcp_registry.save_registry(registry)

    if args.vendor_only:
        print(f"vendor-only: registered {len(entries)} entries, no scanning done")
        return

    print(f"scanning {len(to_scan)} entries ({len(entries) - len(to_scan)} already scanned, skipped)")
    ok = failed = 0
    for i, (entry_id, owner, repo, url, name) in enumerate(to_scan, start=1):
        fetch = rate_limited_github_fetcher(owner, repo, limiter)
        scanned_at = datetime.date.today().isoformat()
        try:
            extracted = scan_entry(fetch, f"{owner}/{repo}")
            mcp_registry.upsert(
                registry,
                {
                    "repo_url": url,
                    "source": "awesome-mcp-servers",
                    "source_key": f"{owner}/{repo}",
                    "name": extracted.get("name") or name,
                    "description": extracted.get("description"),
                    "list_entry_name": name,
                    "scanned_at": scanned_at,
                    "manifest_source": extracted.get("source_file"),
                    "registry_type": extracted.get("registry_type"),
                    "package_identifier": extracted.get("package_identifier"),
                    "package_url": extracted.get("package_url"),
                    "deployment": extracted.get("deployment"),
                    "env_vars_json": extracted.get("env_vars_json"),
                },
                index=index,
            )
            ok += 1
            status = "ok"
        except Exception as e:
            # Broad catch deliberately -- a single dead/malformed entry (404s
            # entirely, unrecognized manifest shape, transient network error)
            # must not kill a run scanning thousands of others. This is the
            # exact per-entry-isolation lesson spike_seed_repo.py's first run
            # surfaced (see MCP_PIPELINE.md's "Spike" section).
            mcp_registry.record_error(registry, entry_id, "awesome-mcp-servers", repr(e), index=index)
            mcp_registry.upsert(
                registry,
                {
                    "repo_url": url,
                    "source": "awesome-mcp-servers",
                    "source_key": f"{owner}/{repo}",
                    "name": name,
                    "scanned_at": scanned_at,
                },
                index=index,
            )
            failed += 1
            status = f"FAILED ({e!r})"

        print(f"[{i}/{len(to_scan)}] {owner}/{repo}: {status}")
        if i % SAVE_EVERY == 0:
            mcp_registry.save_registry(registry)

    mcp_registry.save_registry(registry)
    print(f"\ndone: {ok} scanned ok, {failed} failed, {len(registry)} total rows in registry")


if __name__ == "__main__":
    main()
