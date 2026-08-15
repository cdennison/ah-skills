#!/usr/bin/env python3
"""Scan an MCP server repo for config and print the extracted entry as JSON
(no persistence yet -- just for eyeballing what extraction finds).

Works two ways:
  - Against a local clone (scan_repo)
  - Directly against GitHub via raw.githubusercontent.com/<owner>/<repo>/HEAD/...
    (scan_github_repo) -- no clone needed. GitHub serves "HEAD" as an alias
    for the default branch, so we don't need to look up main vs master.

Extraction priority:
  1. server.json (official MCP registry manifest -- name, description,
     repository, packages[].registryType/transport/version, env vars)
  2. package.json fallback (name, description, repository, engines)

package_url (e.g. npmjs.com/pypi.org landing page) is derived from
registryType + identifier; if that's unavailable, falls back to a direct
link found in README.md.

Usage:
    python scan_mcp.py <path-to-cloned-repo>
    python scan_mcp.py --github <owner>/<repo>
"""

import argparse
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

RAW_GITHUB_URL = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
GITHUB_REPO_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")

# Package registries a server.json/package.json registryType can map to a
# browsable landing page for. OCI images vary by host (ghcr.io, Docker Hub,
# etc.) so identifiers there are usually already a full ref -- not templated.
REGISTRY_URL_TEMPLATES = {
    "npm": "https://www.npmjs.com/package/{id}",
    "pypi": "https://pypi.org/project/{id}/",
    "nuget": "https://www.nuget.org/packages/{id}",
    "crates": "https://crates.io/crates/{id}",
}

README_LINK_RES = {
    "npm": re.compile(r"https://www\.npmjs\.com/package/([\w@./-]+)"),
    "pypi": re.compile(r"https://pypi\.org/project/([\w.-]+)"),
    "nuget": re.compile(r"https://www\.nuget\.org/packages/([\w.-]+)"),
    "crates": re.compile(r"https://crates\.io/crates/([\w.-]+)"),
}

# A fetcher takes a relative path (e.g. "server.json") and returns its text
# content, or None if the file doesn't exist. scan_entry() is agnostic to
# whether that's a local read or an HTTP GET.
Fetcher = Callable[[str], str | None]


def local_fetcher(repo_path: Path) -> Fetcher:
    def fetch(path: str) -> str | None:
        full = repo_path / path
        if not full.exists():
            return None
        try:
            return full.read_text(errors="ignore")
        except OSError:
            return None

    return fetch


def github_fetcher(owner: str, repo: str) -> Fetcher:
    def fetch(path: str) -> str | None:
        url = RAW_GITHUB_URL.format(owner=owner, repo=repo, path=path)
        try:
            with urllib.request.urlopen(url) as resp:
                return resp.read().decode(errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    return fetch


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL in any common form
    (https://github.com/x/y, git+https://.../y.git, github.com/x/y)."""
    match = GITHUB_REPO_RE.search(url)
    if not match:
        raise ValueError(f"not a recognizable GitHub repo URL: {url}")
    return match.group(1), match.group(2)


def derive_package_url(registry_type: str | None, package_identifier: str | None) -> str | None:
    """Build a browsable registry landing page URL from a registryType +
    identifier pair (e.g. npm/"mongodb-mcp-server" -> npmjs.com/package/...)."""
    if not registry_type or not package_identifier:
        return None
    template = REGISTRY_URL_TEMPLATES.get(registry_type)
    if not template:
        return None
    return template.format(id=package_identifier)


def find_readme_package_links(fetch: Fetcher) -> dict[str, str]:
    """Fallback/corroboration: scan README.md for registry links directly,
    for repos without a server.json (or to sanity-check a derived URL)."""
    text = fetch("README.md")
    if text is None:
        return {}
    found = {}
    for registry_type, pattern in README_LINK_RES.items():
        match = pattern.search(text)
        if match:
            found[registry_type] = match.group(0)
    return found


def _read_json(fetch: Fetcher, path: str) -> dict | None:
    text = fetch(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_from_server_json(data: dict, source_file: str) -> dict:
    # A server.json can list installable packages (packages[]), remote
    # endpoints (remotes[]), both, or -- per the MCP registry schema --
    # neither is guaranteed. Treating a missing packages[] as "no package
    # info" used to silently fall through to README-link scraping and
    # attribute some unrelated npm package (e.g. a "run this via mcp-remote"
    # snippet) to the server. Distinguish the cases explicitly instead: a
    # remotes-only entry (e.g. atlassian/atlassian-mcp-server -- the repo is
    # just client config for a closed-source server hosted at
    # mcp.atlassian.com) has no installable package at all, and that's a
    # real, reportable fact, not a gap to paper over with a guess.
    packages = data.get("packages") or []
    remotes = data.get("remotes") or []
    pkg = packages[0] if packages else {}
    transport = pkg.get("transport", {})
    env_vars = pkg.get("environmentVariables", [])

    if packages and remotes:
        # Both an installable package AND a separate hosted endpoint --
        # e.g. daedalusdevelopmentgroup/ddg-agent-payable-services ships a
        # pypi package (stdio) alongside a streamable-http remote at
        # mcp.daedalusdevelopmentgroup.com. This matches Glama's own
        # "hosting:hybrid" attribute (confirmed via their /v1/attributes
        # endpoint) -- worth keeping as a distinct third state rather than
        # collapsing it into "local" and silently losing the fact a remote
        # endpoint also exists.
        deployment = "hybrid"
    elif packages:
        # A package can itself be a thin stdio wrapper around a remote
        # backend (transport type streamable-http/sse rather than stdio) --
        # that's still "remote" in the sense that matters here (nothing
        # runs locally except a proxy), so key off transport type, not just
        # packages[] presence.
        deployment = "local" if transport.get("type") in (None, "stdio") else "remote"
    elif remotes:
        deployment = "remote"
    else:
        deployment = None

    return {
        "name": data.get("name"),
        "description": data.get("description"),
        "repo_url": (data.get("repository") or {}).get("url"),
        "version": data.get("version"),
        "registry_type": pkg.get("registryType") if packages else None,
        "transport": transport.get("type") if isinstance(transport, dict) else transport,
        "package_identifier": pkg.get("identifier") if packages else None,
        "env_vars_json": json.dumps(env_vars),
        "source_file": source_file,
        "deployment": deployment,
        "remote_urls": [r.get("url") for r in remotes] if remotes else None,
        "has_installable_package": bool(packages),
    }


def extract_from_package_json(data: dict, source_file: str) -> dict:
    repo = data.get("repository")
    repo_url = repo.get("url") if isinstance(repo, dict) else repo
    return {
        "name": data.get("mcpName") or data.get("name"),
        "description": data.get("description"),
        "repo_url": repo_url,
        "version": data.get("version"),
        "registry_type": "npm" if "bin" in data or data.get("main") else None,
        "transport": None,
        "package_identifier": data.get("name"),
        "env_vars_json": None,
        "source_file": source_file,
    }


def scan_entry(fetch: Fetcher, label: str) -> dict:
    """Shared extraction logic: given a Fetcher (local or GitHub-raw) and a
    human-readable label for the source (repo path or "owner/repo"), return
    the extracted entry dict. Raises ValueError if no manifest is found."""
    server_json = _read_json(fetch, "server.json")
    if server_json is not None:
        entry = extract_from_server_json(server_json, "server.json")
    else:
        package_json = _read_json(fetch, "package.json")
        if package_json is None:
            raise ValueError(f"no server.json or package.json found in {label}")
        entry = extract_from_package_json(package_json, "package.json")

    if not entry["name"]:
        raise ValueError(f"could not determine MCP server name from {label}")

    package_url = derive_package_url(entry["registry_type"], entry["package_identifier"])
    # A server.json that explicitly declares "no installable package" (only
    # remotes[], e.g. atlassian-mcp-server) means there genuinely is no
    # package_url -- don't paper over that by grabbing whatever unrelated
    # registry link happens to appear in the README (install snippets often
    # link a generic proxy tool like mcp-remote, not the server itself).
    if package_url is None and entry.get("has_installable_package", True):
        # No (or unrecognized) registryType -- fall back to whatever the
        # README links to directly.
        readme_links = find_readme_package_links(fetch)
        if readme_links:
            registry_type, package_url = next(iter(readme_links.items()))
            entry["registry_type"] = entry["registry_type"] or registry_type
    entry["package_url"] = package_url

    return entry


def scan_repo(repo_path: Path) -> dict:
    """Extract an MCP server entry from a local clone at repo_path."""
    entry = scan_entry(local_fetcher(repo_path), str(repo_path))
    entry["repo_path"] = str(repo_path)
    return entry


def scan_github_repo(owner: str, repo: str) -> dict:
    """Extract an MCP server entry directly from GitHub, no clone required."""
    entry = scan_entry(github_fetcher(owner, repo), f"{owner}/{repo}")
    entry["repo_path"] = None
    entry.setdefault("repo_url", f"https://github.com/{owner}/{repo}")
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Local repo path, or owner/repo when --github is set")
    parser.add_argument(
        "--github",
        action="store_true",
        help="Treat target as a GitHub owner/repo and fetch via raw.githubusercontent.com (no clone)",
    )
    args = parser.parse_args()

    if args.github:
        owner, repo = args.target.split("/", 1)
        entry = scan_github_repo(owner, repo)
    else:
        entry = scan_repo(Path(args.target))
    print(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
