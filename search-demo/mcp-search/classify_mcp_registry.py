#!/usr/bin/env python3
"""Classify every mcp-repo-seeds/registry.json row as an MCP server, client,
framework, tooling, or unclear -- the registry-wide equivalent of
classify_mcp.py, sharing its keyword vocabulary (shared/mcp_keywords.py) but
adapted to this pipeline's schema and, more importantly, its different
population.

**Why the default differs from classify_mcp.py's**: that script classifies
raw npm "mcp"-keyword search hits -- a noisy mix of servers, SDKs, clients,
and middleware -- so its safe default absent a positive signal is
"unclear". Every row in *this* registry came from a source that already
curates MCP servers specifically (the official MCP registry, Glama's
directory, or the awesome-mcp-servers seed list) -- so here, absent a
signal pointing elsewhere (framework/client/tooling keyword), the safe
default is "server", tagged mcp_category_source="source-default" so it
stays clearly distinguishable from a keyword-confirmed "rule" classification
in every downstream consumer (CSV, stats). Only rows with no name/
description/readme text at all fall through to "unclear".

Classification text = name + description, matching classify_mcp.py's own
scope -- NOT the full readme. First cut of this script pulled in up to 4000
chars of readme for every row and it backfired immediately: readmes
routinely say things like "compatible with any MCP client" or list Claude
Desktop/Cursor as "MCP clients" that connect *to* the server being
documented -- CLIENT_WORD_RE matched that bare "client" mention and
mis-tagged thousands of obvious servers as "client" (verified against real
rows: every sampled false positive had a description starting "MCP server
for ..." or similar). name+description is short and authoritative the same
way it is for classify_mcp.py, so keyword signals stay reliable there. A
readme is only consulted as a fallback -- and only its first 500 characters
(title/tagline territory) -- for the minority of rows with no description
at all, never to override a signal name+description already gave.

Signals, in priority order:
  1. name matches a known third-party framework pattern -> framework
  2. text matches "mcp server"/"mcp-server" and doesn't also match the
     client-keyword phrase below -> server
  3. text self-identifies as a client -- "a"/"an"/"is" + "mcp client"
     (singular only; see shared/mcp_keywords.CLIENT_KEYWORD_RE) -> client
  4. a tooling keyword (adapter/middleware/proxy/instrumentation/...)
     without a stronger server signal -> tooling
  5. some text exists but nothing matched -> server (source-default)
  6. no text at all -> unclear

Uses phrase-level client matching (CLIENT_KEYWORD_RE), not classify_mcp.py's
bare CLIENT_WORD_RE -- validated against real data that the bare word is far
too noisy here (see the note on text scope above and CLIENT_KEYWORD_RE's own
docstring in shared/mcp_keywords.py for the concrete false positives found).

Pure in-memory pass, no network calls -- safe to run concurrently with
pull_*.py scripts in the sense that it doesn't hit any external API, but
NOT safe to run at the exact same moment as another script that also
loads-modifies-saves registry.json (last save wins, per mcp_registry.py's
plain read/write -- there's no lock). Pause any other writer first.

Usage:
    python classify_mcp_registry.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from shared.mcp_keywords import CLIENT_KEYWORD_RE, SERVER_KEYWORD_RE, THIRD_PARTY_FRAMEWORK_RE, TOOLING_KEYWORD_RE

# Lead-paragraph-only fallback (title/tagline), not the full document --
# see the module docstring for why deep readme text (install instructions,
# client-compatibility tables, badges) is unreliable for keyword matching.
README_FALLBACK_CHARS = 500


def gather_text(entry: dict) -> tuple[str, bool]:
    name = entry.get("name") or ""
    description = entry.get("description") or ""
    used_readme = False

    if not description.strip():
        readme_path = entry.get("readme_path")
        if readme_path:
            full_path = mcp_registry.MCP_DIR.parent / readme_path
            if full_path.exists():
                description = full_path.read_text(errors="ignore")[:README_FALLBACK_CHARS]
                used_readme = True

    return f"{name} {description}", used_readme


def classify(entry: dict) -> dict:
    text, used_readme = gather_text(entry)
    bare_name = (entry.get("name") or "").strip()
    # Handle both npm-style "@scope/pkg" and official-registry-style
    # "io.github.owner/repo-name" reverse-DNS names -- the last "/" segment
    # is what actually carries the framework name in either shape.
    short_name = bare_name.rsplit("/", 1)[-1]

    is_framework = bool(short_name) and bool(THIRD_PARTY_FRAMEWORK_RE.match(short_name))

    # Real descriptions routinely contain both phrases at once -- "An MCP
    # server for X. It lets an MCP client issue Y." -- self-describing as a
    # server, then mentioning what connects to it. When both match, whichever
    # phrase appears first in the text wins: the self-description reliably
    # comes first, the other phrase is describing what it works with.
    server_match = SERVER_KEYWORD_RE.search(text)
    client_match = CLIENT_KEYWORD_RE.search(text)
    if server_match and client_match:
        server_keyword_hit = server_match.start() <= client_match.start()
        client_keyword_hit = not server_keyword_hit
    else:
        server_keyword_hit = bool(server_match)
        client_keyword_hit = bool(client_match)

    tooling_keyword_hit = bool(TOOLING_KEYWORD_RE.search(text))

    signals = {
        "is_third_party_framework": is_framework,
        "server_keyword_hit": server_keyword_hit,
        "client_keyword_hit": client_keyword_hit,
        "tooling_keyword_hit": tooling_keyword_hit,
        "used_readme": used_readme,
    }

    if is_framework:
        category, source = "framework", "rule"
    elif server_keyword_hit:
        category, source = "server", "rule"
    elif client_keyword_hit:
        category, source = "client", "rule"
    elif tooling_keyword_hit:
        category, source = "tooling", "rule"
    elif text.strip():
        category, source = "server", "source-default"
    else:
        category, source = "unclear", "rule"

    return {"mcp_category": category, "mcp_category_source": source, "mcp_category_signals": signals}


def main():
    registry = mcp_registry.load_registry()

    counts: Counter = Counter()
    source_default_count = 0
    used_readme_count = 0
    for entry in registry:
        entry.update(classify(entry))
        counts[entry["mcp_category"]] += 1
        if entry["mcp_category_source"] == "source-default":
            source_default_count += 1
        if entry["mcp_category_signals"]["used_readme"]:
            used_readme_count += 1

    mcp_registry.save_registry(registry)

    print(f"classified {len(registry):,} rows ({used_readme_count:,} using readme text, rest name+description only)\n")
    for category, count in counts.most_common():
        print(f"  {category:<12} {count:,}")
    print(f"\nof which 'server' by source-default (no explicit keyword signal either way): {source_default_count:,}")


if __name__ == "__main__":
    main()
