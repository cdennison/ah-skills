"""Hermetic tests for the install-command parser (no network)."""

import pytest

from extract_packages import extract_packages_from_line


@pytest.mark.parametrize(
    ("line", "ecosystem", "expected"),
    [
        # the context7 shape: command after `claude mcp add ... --`
        ("claude mcp add context7 -- npx -y @upstash/context7-mcp", "npm", ["@upstash/context7-mcp"]),
        ("claude mcp add exa -e KEY=x exa -- npx -y exa-mcp-server", "npm", ["exa-mcp-server"]),
        # inline backticks
        ("install it with `npm i -g @runcomfy/cli` first", "npm", ["@runcomfy/cli"]),
        ("run `pip install ruff` then `pipx run black .`", "pip", ["ruff", "black"]),
        # line-leading, with a shell prompt
        ("$ pip3 install --user httpx rich", "pip", ["httpx", "rich"]),
        # runner verbs take only the first token (not subcommands / args)
        ("npx create-react-app my-app", "npm", ["create-react-app"]),
        ("uvx ruff check .", "pip", ["ruff"]),
        ("npx playwright install chromium", "npm", ["playwright"]),
        # prose must NOT be parsed as a command
        ("treat every skill like an npm install of untrusted code", "npm", []),
        ("you may need to pip install things", "pip", []),
        # ecosystem isolation
        ("npm i -g typescript", "pip", []),
        ("pip install numpy", "npm", []),
        # scoped + versioned specs
        ("`npm install @scope/pkg@1.2.3`", "npm", ["@scope/pkg"]),
        ("`pip install 'httpx>=0.27,<1.0'`", "pip", ["httpx"]),
        # local paths / requirements files are not packages
        ("pip install -r requirements.txt", "pip", []),
        ("pip install ./my-local-pkg", "pip", []),
    ],
)
def test_extract(line, ecosystem, expected):
    assert extract_packages_from_line(line, ecosystem) == expected


def test_pip_normalizes_case():
    # pip package names are case-insensitive -> normalized to lowercase so the
    # PyPI lookup and the skill-mention join agree regardless of how a given
    # SKILL.md capitalized it.
    assert extract_packages_from_line("pip install Django", "pip") == ["django"]
    assert extract_packages_from_line("uv pip install Flask-Cors", "pip") == ["flask-cors"]
