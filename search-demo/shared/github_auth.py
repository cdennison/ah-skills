"""Shared GitHub Personal Access Token loading + auth-header/clone helpers.
Used by every script (in mcp-search/ and, potentially, elsewhere) that hits
raw.githubusercontent.com, api.github.com, or does a `git clone` of a GitHub
repo -- so the token is read once, the same way, everywhere, instead of each
caller re-implementing its own .env parsing.

Token is looked for, in order:
  1. the GITHUB_PAT environment variable
  2. .env.local (repo root)
  3. .env (repo root)

.env already exists at the repo root with a working GITHUB_PAT -- it's what
../clone_repos.py and ../search_github.py already read theirs from (see
../.env.example). .env.local is checked *first* so a token placed there
takes precedence, but falling through to the existing .env means this
module works immediately against the token that's already configured,
without requiring a second copy of the same secret. Both files are already
gitignored.

No scopes required -- a PAT only raises the anonymous ceiling (GitHub API:
60/hr -> 5000/hr; ../clone_repos.py's own comment on this). Every function
here degrades to unauthenticated behavior if no token is found anywhere,
rather than raising -- callers still work, just at the lower rate.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_LOCAL_FILE = ROOT / ".env.local"
ENV_FILE = ROOT / ".env"

# Hosts a GITHUB_PAT should be attached to. Never attach it to unrelated
# APIs (glama.ai, registry.modelcontextprotocol.io) -- see shared/http.py.
GITHUB_HOSTS = {"api.github.com", "raw.githubusercontent.com", "codeload.github.com"}


def _read_env_file(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def load_github_pat() -> str | None:
    token = os.environ.get("GITHUB_PAT")
    if token:
        return token
    for path in (ENV_LOCAL_FILE, ENV_FILE):
        token = _read_env_file(path, "GITHUB_PAT")
        if token:
            return token
    return None


GITHUB_PAT = load_github_pat()


def auth_headers() -> dict[str, str]:
    """Bearer-token header for api.github.com / raw.githubusercontent.com
    requests. Empty dict if no token is configured."""
    return {"Authorization": f"Bearer {GITHUB_PAT}"} if GITHUB_PAT else {}


def git_clone_extraheader_args() -> list[str]:
    """`-c http.extraheader=...` args for `git clone`, authenticating the
    clone without the token ever appearing in the URL, the cloned repo's
    .git/config, or `ps` output -- same approach ../clone_repos.py uses."""
    if not GITHUB_PAT:
        return []
    basic = base64.b64encode(f"x-access-token:{GITHUB_PAT}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def rate_limit_status() -> dict | None:
    """Query api.github.com/rate_limit (authenticated if a token is
    configured) -- lets a caller confirm the token is actually raising its
    ceiling before kicking off a long run. Returns None on any failure."""
    req = urllib.request.Request(
        "https://api.github.com/rate_limit",
        headers={"Accept": "application/vnd.github+json", **auth_headers()},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
