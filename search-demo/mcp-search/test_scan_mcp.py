import json
from pathlib import Path

import pytest

from scan_mcp import (
    derive_package_url,
    extract_from_package_json,
    extract_from_pyproject,
    extract_from_server_json,
    find_readme_package_links,
    local_fetcher,
    parse_github_repo_url,
    scan_entry,
    scan_repo,
)

REPO_DIR = Path(__file__).parent / "mongodb-mcp-server"


def test_scan_real_clone():
    if not REPO_DIR.exists():
        pytest.skip("mongodb-mcp-server not cloned into mcp-search")

    entry = scan_repo(REPO_DIR)

    assert entry["name"] == "io.github.mongodb-js/mongodb-mcp-server"
    assert "MongoDB" in entry["description"]
    assert entry["repo_url"] == "https://github.com/mongodb-js/mongodb-mcp-server"
    assert entry["registry_type"] == "npm"
    assert entry["transport"] == "stdio"
    assert entry["package_identifier"] == "mongodb-mcp-server"
    assert entry["package_url"] == "https://www.npmjs.com/package/mongodb-mcp-server"
    assert entry["source_file"] == "server.json"

    env_vars = json.loads(entry["env_vars_json"])
    assert any(v["name"] == "MDB_MCP_API_CLIENT_ID" for v in env_vars)


def test_extract_from_server_json_minimal():
    data = {
        "name": "example/server",
        "description": "An example server",
        "repository": {"url": "https://github.com/example/server"},
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "example-server",
                "transport": {"type": "stdio"},
                "environmentVariables": [],
            }
        ],
    }
    entry = extract_from_server_json(data, "server.json")
    assert entry["name"] == "example/server"
    assert entry["registry_type"] == "npm"
    assert entry["transport"] == "stdio"


def test_extract_from_package_json_fallback():
    data = {
        "name": "example-server",
        "mcpName": "io.github.example/example-server",
        "description": "Example MCP server",
        "repository": {"url": "https://github.com/example/example-server.git"},
        "version": "0.1.0",
        "bin": {"example-server": "dist/index.js"},
    }
    entry = extract_from_package_json(data, "package.json")
    assert entry["name"] == "io.github.example/example-server"
    assert entry["registry_type"] == "npm"


PYPROJECT_TOML = """\
[project]
name = "daytona-mcp-interpreter"
version = "0.1.1"
description = "A Daytona MCP server for Python code interpretation"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]>=1.0.0",
    "pydantic>=2.10.6",
    "httpx>=0.24.0",
    "pytest>=8 ; extra == 'dev'",
]

[project.scripts]
daytona-interpreter = "daytona_mcp_interpreter.server:main"

[build-system]
requires = ["hatchling"]
"""


def test_extract_from_pyproject():
    import tomllib

    entry = extract_from_pyproject(tomllib.loads(PYPROJECT_TOML), "pyproject.toml")

    assert entry["name"] == "daytona-mcp-interpreter"
    assert entry["registry_type"] == "pypi"
    assert entry["package_identifier"] == "daytona-mcp-interpreter"
    assert entry["deployment"] == "local"
    assert entry["has_installable_package"] is True
    assert entry["console_scripts"] == ["daytona-interpreter"]
    # env-marker-gated requirement (pytest) is dropped; the rest keep just
    # the distribution name, no version specifier.
    assert entry["pyproject_dependencies"] == ["mcp", "pydantic", "httpx"]


def test_scan_entry_falls_back_to_pyproject():
    """A repo with neither server.json nor package.json but a pyproject.toml
    (PyPI/uv/pipx MCP server) must extract, not raise."""
    files = {"pyproject.toml": PYPROJECT_TOML}
    entry = scan_entry(lambda path: files.get(path), "nibzard/daytona-mcp-interpreter")

    assert entry["package_identifier"] == "daytona-mcp-interpreter"
    assert entry["registry_type"] == "pypi"
    assert entry["package_url"] == "https://pypi.org/project/daytona-mcp-interpreter/"


def test_scan_entry_still_raises_when_no_manifest_at_all():
    with pytest.raises(ValueError):
        scan_entry(lambda path: None, "someone/empty-repo")


def test_derive_package_url():
    assert derive_package_url("npm", "mongodb-mcp-server") == "https://www.npmjs.com/package/mongodb-mcp-server"
    assert derive_package_url("pypi", "example-server") == "https://pypi.org/project/example-server/"
    assert derive_package_url(None, "example-server") is None
    assert derive_package_url("npm", None) is None
    assert derive_package_url("oci", "ghcr.io/example/server") is None


def test_find_readme_package_links(tmp_path):
    (tmp_path / "README.md").write_text(
        "Install from https://pypi.org/project/example-server and see docs."
    )
    links = find_readme_package_links(local_fetcher(tmp_path))
    assert links["pypi"] == "https://pypi.org/project/example-server"


def test_scan_repo_raises_without_manifest(tmp_path):
    with pytest.raises(ValueError):
        scan_repo(tmp_path)


def test_remotes_only_server_json_has_no_installable_package():
    """atlassian/atlassian-mcp-server edge case: server.json with only
    remotes[] (a closed-source, cloud-hosted server) and no packages[] --
    must not report a package_url at all, and must not fall back to
    scraping an unrelated registry link out of README.md."""
    data = {
        "name": "com.atlassian/atlassian-mcp-server",
        "description": "Connect to Atlassian Jira, Confluence, and Compass.",
        "repository": {"url": "https://github.com/atlassian/atlassian-mcp-server"},
        "version": "1.1.3",
        "remotes": [{"type": "streamable-http", "url": "https://mcp.atlassian.com/v1/mcp"}],
    }
    files = {
        "server.json": json.dumps(data),
        # Install snippets in READMEs for remote-only servers often mention
        # a generic proxy tool -- must not be attributed to this server.
        "README.md": "Run via `npx -y mcp-remote https://mcp.atlassian.com/v1/mcp`.",
    }
    entry = scan_entry(lambda path: files.get(path), "atlassian/atlassian-mcp-server")

    assert entry["has_installable_package"] is False
    assert entry["deployment"] == "remote"
    assert entry["package_url"] is None
    assert entry["remote_urls"] == ["https://mcp.atlassian.com/v1/mcp"]


def test_hybrid_deployment_when_packages_and_remotes_both_present():
    """daedalusdevelopmentgroup/ddg-agent-payable-services (found via the
    awesome-mcp-servers seed repo spike): ships an installable pypi package
    (stdio) AND a separate hosted streamable-http endpoint -- matches
    Glama's own "hosting:hybrid" attribute, must not collapse to "local"
    and silently drop the remote endpoint."""
    data = {
        "name": "io.github.example/hybrid-server",
        "description": "Example hybrid server.",
        "repository": {"url": "https://github.com/example/hybrid-server"},
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "pypi",
                "identifier": "hybrid-server",
                "transport": {"type": "stdio"},
                "environmentVariables": [],
            }
        ],
        "remotes": [{"type": "streamable-http", "url": "https://hybrid-server.example.com/mcp"}],
    }
    entry = extract_from_server_json(data, "server.json")

    assert entry["deployment"] == "hybrid"
    assert entry["has_installable_package"] is True
    assert entry["remote_urls"] == ["https://hybrid-server.example.com/mcp"]


def test_parse_github_repo_url():
    assert parse_github_repo_url("https://github.com/mongodb-js/mongodb-mcp-server") == (
        "mongodb-js",
        "mongodb-mcp-server",
    )
    assert parse_github_repo_url("git+https://github.com/example/example-server.git") == (
        "example",
        "example-server",
    )
    with pytest.raises(ValueError):
        parse_github_repo_url("not a url")


def test_scan_entry_with_fake_github_fetcher():
    """scan_github_repo() itself hits the network, so exercise the shared
    scan_entry() logic against a fake fetcher instead of a real HTTP call."""
    files = {
        "server.json": json.dumps(
            {
                "name": "example/server",
                "description": "Example",
                "repository": {"url": "https://github.com/example/server"},
                "version": "1.0.0",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "example-server",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [],
                    }
                ],
            }
        )
    }
    entry = scan_entry(lambda path: files.get(path), "example/server")
    assert entry["name"] == "example/server"
    assert entry["package_url"] == "https://www.npmjs.com/package/example-server"
