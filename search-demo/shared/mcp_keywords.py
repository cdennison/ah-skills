"""Shared keyword/regex vocabulary for classifying MCP-adjacent text (name +
description + readme) as server/client/framework/tooling.

Used by both mcp-search/classify_mcp.py (npm "mcp"-search candidates, a
noisy mix of servers/SDKs/clients/middleware) and
mcp-search/classify_mcp_registry.py (the multi-source registry) --
extracted here because both need the identical regexes and third-party
framework name list; a fix to one (e.g. the mcp-server hyphenation bug
documented in MCP_PIPELINE.md -- the original regex was space-only and
missed "fiori-mcp-server") now applies to both instead of silently
diverging.
"""

import re

THIRD_PARTY_FRAMEWORK_RE = re.compile(r"^(mcp-framework|fastmcp|tmcp|@tmcp/.*|@rekog/mcp-nest)$")

TOOLING_KEYWORD_RE = re.compile(
    r"\b(adapter|middleware|instrumentation|inspector|proxy|tunnel|utils?|plugin|toolkit|"
    r"cli|generator|framework|sdk|client)\b",
    re.IGNORECASE,
)
# Hyphen OR space between "mcp"/"server" -- package names use "mcp-server",
# prose uses "mcp server".
SERVER_KEYWORD_RE = re.compile(r"mcp[\s-]server|server[\s-]mcp|model context protocol[\s-]server", re.IGNORECASE)
# A bare "client" mention nearby ("...connect to MCP servers..." in a package
# that calls *itself* an "MCP client") means a server-keyword match is
# describing what the package talks to, not what it is -- suppress the
# server match in that case rather than trust a raw substring hit. Reliable
# for classify_mcp.py's short npm description text; NOT reliable on its own
# for longer/free-form text -- see CLIENT_KEYWORD_RE below.
CLIENT_WORD_RE = re.compile(r"\bclient\b", re.IGNORECASE)

# Self-identifying "client" match: requires a singular "mcp client"
# immediately preceded by "a"/"an"/"is" (the shape a project uses to
# describe *itself* -- "X is an MCP client that...", "a lightweight MCP
# client for...") and excludes the plural. This is an allowlist, not
# classify_mcp.py's bare-word CLIENT_WORD_RE, because validating against
# real registry data showed the overwhelming majority of "client" mentions
# in MCP server descriptions are audience/compatibility language --
# "enables MCP clients to...", "for MCP clients", "gives MCP clients
# access", "compatible with any MCP client" -- describing what connects TO
# the thing being described, not what it is. classify_mcp.py's bare-word
# approach worked there only because has_server_sdk_dep already resolved
# most rows before that check mattered; classify_mcp_registry.py has no
# equivalent dependency signal, so the noisy version mis-tagged hundreds of
# obvious servers as "client" in testing (e.g. "MCP server that gives MCP
# clients access..." -- literally contains "MCP server" but lost to the
# bare-word client suppression). The (?!s) keeps "MCP clients" (plural)
# from matching even when preceded by "a"/"an"/"is" in some contorted
# phrasing.
CLIENT_KEYWORD_RE = re.compile(r"\b(?:a|an|is)\s+(?:[\w'-]+\s+){0,2}mcp[\s-]client\b(?!s)", re.IGNORECASE)
