"""Manual overrides for packages classify_mcp.py's deterministic rules
couldn't confidently resolve ("unclear"). Each entry is a human (well,
Claude, having actually read the readme) judgment call, kept separate from
the deterministic classifier so provenance is always clear -- see
mcp_category_source in the output ("rule" vs "manual").

Extend this as classify_mcp.py surfaces new unclear packages. If a rule
improvement later resolves one of these deterministically, it's fine to
leave the manual entry in place -- classify_mcp.py only consults it when
the deterministic pass still lands on "unclear", so a fixed rule silently
takes precedence.
"""

MANUAL_CLASSIFICATIONS = {
    "@playwright/mcp": {
        "category": "server",
        "opinion": (
            "Yes -- readme states outright: \"A Model Context Protocol (MCP) server that provides "
            "browser automation capabilities using Playwright.\" Ships a bin (playwright-mcp) too. "
            "The classifier missed it only because the description text (\"Playwright Tools for MCP\") "
            "doesn't contain the literal \"mcp server\" phrase."
        ),
    },
    "@univerjs-pro/mcp": {
        "category": "server",
        "opinion": (
            "Likely yes, with lower confidence -- no @modelcontextprotocol/* dependency and no bin, "
            "but it's the base MCP-integration package that @univerjs-pro/sheets-mcp builds on inside "
            "the Univer office-suite monorepo. Reads as an embedded MCP server capability for the Univer "
            "platform rather than a standalone npx-run server -- readme is just the generic Univer SDK "
            "banner, no MCP-specific detail to confirm further without reading source."
        ),
    },
    "@univerjs-pro/sheets-mcp": {
        "category": "server",
        "opinion": (
            "Likely yes, same basis as @univerjs-pro/mcp -- depends directly on it plus Univer's sheets "
            "modules, appears to be the sheets-specific MCP tool layer. Same caveat: no deterministic "
            "signal, generic readme, embedded-in-platform rather than standalone."
        ),
    },
    "mcp-auth": {
        "category": "tooling",
        "opinion": (
            "No -- readme opens with \"MCP Auth Node.js SDK\" and describes itself as a "
            "production-ready OAuth 2.1/OIDC provider integration plus libraries/tutorials for adding "
            "auth *to* MCP servers. It's an auth library MCP servers depend on, not an MCP server itself."
        ),
    },
}
