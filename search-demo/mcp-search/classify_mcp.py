#!/usr/bin/env python3
"""Classify each npm_mcp_candidates.json entry as an MCP server, client,
adjacent tooling, or unclear -- using deterministic signals only (no LLM
judgment call per package).

Background: the original has_mcp_sdk_dependency check (fetch_npm_mcp_candidates.py)
only looked at `dependencies` for the literal string "@modelcontextprotocol/sdk".
That misses two real patterns seen in the data:
  - The MCP SDK went v2 and split into subpackages (@modelcontextprotocol/server,
    /client, /core, /node, /express, /hono, /ext-apps) -- "sdk" alone under-counts.
  - Nest/DI-style packages (@rekog/mcp-nest) declare the SDK as a
    peerDependency, not a dependency -- dependencies-only misses those too.

Signals used, each deterministic and re-checkable from raw npm data:
  - official SDK server-side packages in dependencies OR peerDependencies
    (@modelcontextprotocol/sdk, /server, /express, /node, /hono) => "server"
  - known third-party MCP server frameworks (mcp-framework, fastmcp, tmcp,
    @tmcp/*) => "server"
  - official SDK client-only package present, no server-side package => "client"
  - a `bin` entry (npm-executable) is a decent corroborating signal for
    "server" (servers are typically launched as a CLI over stdio/http);
    absence of bin on an otherwise-ambiguous package pushes toward "tooling"
  - name/description keyword matches for adapter/middleware/instrumentation/
    inspector/proxy/tunnel/utils/plugin => "tooling" (MCP-adjacent, not itself
    a server)
  - none of the above => "unclear" (needs human review)

Usage:
    python classify_mcp.py
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manual_classifications import MANUAL_CLASSIFICATIONS
from shared.mcp_keywords import CLIENT_WORD_RE, SERVER_KEYWORD_RE, THIRD_PARTY_FRAMEWORK_RE, TOOLING_KEYWORD_RE
from shared.rate_limit import sleep_if_more

DATA_PATH = Path(__file__).parent / "npm_mcp_candidates.json"
REQUEST_INTERVAL_SECONDS = 2.0

SERVER_SIGNAL_PACKAGES = {
    "@modelcontextprotocol/sdk",
    "@modelcontextprotocol/server",
    "@modelcontextprotocol/express",
    "@modelcontextprotocol/node",
    "@modelcontextprotocol/hono",
}
CLIENT_SIGNAL_PACKAGES = {"@modelcontextprotocol/client"}


def fetch_full_detail(name: str) -> dict:
    encoded = urllib.parse.quote(name, safe="")
    with urllib.request.urlopen(f"https://registry.npmjs.org/{encoded}") as resp:
        doc = json.load(resp)
    latest = doc.get("dist-tags", {}).get("latest")
    return (doc.get("versions") or {}).get(latest, {})


def classify(entry: dict, deps: dict, peer_deps: dict, bin_: dict | str | None, keywords: list | None) -> dict:
    all_pkgs = set(deps) | set(peer_deps)
    has_server_sdk = bool(all_pkgs & SERVER_SIGNAL_PACKAGES)
    has_client_sdk = bool(all_pkgs & CLIENT_SIGNAL_PACKAGES)
    # "is this package itself a known MCP framework" vs "does it merely use
    # one" -- @storybook/mcp depends on tmcp, but that makes it a server
    # built with tmcp, not tmcp itself. Only the name match means "this IS
    # the framework"; a dependency match is folded into the server signal.
    is_framework = bool(THIRD_PARTY_FRAMEWORK_RE.match(entry["name"]))
    uses_framework_dep = any(THIRD_PARTY_FRAMEWORK_RE.match(p) for p in all_pkgs)
    if uses_framework_dep:
        has_server_sdk = True
    has_bin = bool(bin_)
    text = f"{entry['name']} {entry.get('description') or ''} {' '.join(keywords or [])}"
    client_word_hit = bool(CLIENT_WORD_RE.search(text))
    server_keyword_hit = bool(SERVER_KEYWORD_RE.search(text)) and not client_word_hit
    tooling_keyword_hit = bool(TOOLING_KEYWORD_RE.search(text))

    signals = {
        "has_server_sdk_dep": has_server_sdk,
        "has_client_sdk_dep": has_client_sdk,
        "is_third_party_framework": is_framework,
        "uses_framework_dep": uses_framework_dep,
        "has_bin": has_bin,
        "server_keyword_hit": server_keyword_hit,
        "tooling_keyword_hit": tooling_keyword_hit,
        "client_word_hit": client_word_hit,
    }

    # Order matters: an explicit "mcp-server" name/description match is the
    # strongest signal (it's how the ecosystem names things) and wins even
    # over a tooling keyword hit (e.g. "openapi-mcp-generator" has "generator"
    # in it but is explicitly building a server) -- unless the text also
    # self-identifies as a "client" (see client_word_hit above), which means
    # the "servers" mention was describing what it talks to, not itself.
    # A dependency on the server-side SDK is necessary-but-not-sufficient on
    # its own -- lots of middleware/tunnels/utils packages pull it in too --
    # so a tooling keyword hit without a bin (nothing to actually launch
    # standalone) demotes those back out of "server".
    if is_framework:
        category = "framework"
    elif server_keyword_hit:
        category = "server"
    elif has_client_sdk and not has_server_sdk:
        category = "client"
    elif not has_server_sdk and client_word_hit:
        category = "client"
    elif has_server_sdk and tooling_keyword_hit and not has_bin:
        category = "tooling"
    elif has_server_sdk:
        category = "server"
    elif tooling_keyword_hit:
        category = "tooling"
    else:
        category = "unclear"

    return {"mcp_category": category, "mcp_category_signals": signals}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Reclassify using already-cached peer_dependencies/bin/keywords instead of re-hitting npm",
    )
    args = parser.parse_args()

    entries = json.loads(DATA_PATH.read_text())
    ambiguous = [e for e in entries if not e.get("has_mcp_sdk_dependency")]
    print(f"{len(ambiguous)}/{len(entries)} entries lack the original deterministic signal; re-checking each")

    for i, entry in enumerate(ambiguous, start=1):
        if args.no_fetch and "peer_dependencies" in entry:
            deps = entry.get("_deps_cache") or {}
            peer_deps = entry.get("peer_dependencies") or {}
            bin_ = entry.get("bin")
            keywords = entry.get("keywords")
            entry.update(classify(entry, deps, peer_deps, bin_, keywords))
            print(f"[{i}/{len(ambiguous)}] {entry['name']}: {entry['mcp_category']} (cached)")
            continue

        try:
            version_doc = fetch_full_detail(entry["name"])
        except urllib.error.HTTPError as e:
            print(f"[{i}/{len(ambiguous)}] {entry['name']}: HTTP {e.code}, leaving unclear")
            entry["mcp_category"] = "unclear"
            entry["mcp_category_signals"] = {"error": f"HTTP {e.code}"}
            sleep_if_more(i, len(ambiguous), REQUEST_INTERVAL_SECONDS)
            continue

        deps = version_doc.get("dependencies") or {}
        peer_deps = version_doc.get("peerDependencies") or {}
        bin_ = version_doc.get("bin")
        keywords = version_doc.get("keywords")

        entry["_deps_cache"] = deps
        entry["peer_dependencies"] = peer_deps
        entry["bin"] = bin_
        entry["keywords"] = keywords or entry.get("keywords")
        entry.update(classify(entry, deps, peer_deps, bin_, entry["keywords"]))

        print(f"[{i}/{len(ambiguous)}] {entry['name']}: {entry['mcp_category']}")
        sleep_if_more(i, len(ambiguous), REQUEST_INTERVAL_SECONDS)

    # Entries that already had the SDK dependency signal are unambiguously servers.
    for entry in entries:
        if entry.get("has_mcp_sdk_dependency") and "mcp_category" not in entry:
            entry["mcp_category"] = "server"
            entry["mcp_category_signals"] = {"has_server_sdk_dep": True}

    # Track provenance before manual overrides so it's always clear which
    # rows are rule-derived vs a human/Claude judgment call.
    for entry in entries:
        entry.setdefault("mcp_category_source", "rule")

    applied = 0
    for entry in entries:
        if entry["mcp_category"] == "unclear" and entry["name"] in MANUAL_CLASSIFICATIONS:
            override = MANUAL_CLASSIFICATIONS[entry["name"]]
            entry["mcp_category"] = override["category"]
            entry["mcp_category_source"] = "manual"
            entry["claude_opinion"] = override["opinion"]
            applied += 1
    if applied:
        print(f"\napplied {applied} manual override(s) from manual_classifications.py")

    DATA_PATH.write_text(json.dumps(entries, indent=2))

    from collections import Counter

    counts = Counter(e["mcp_category"] for e in entries)
    print("\ncategory counts:", dict(counts))

    unclear = [e["name"] for e in entries if e["mcp_category"] == "unclear"]
    if unclear:
        print(f"\nstill unclear ({len(unclear)}), needs human review:")
        for name in unclear:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
