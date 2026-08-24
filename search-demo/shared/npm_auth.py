"""Shared npm registry access-token loading + auth-header helper. Same
shape as shared/github_auth.py, deliberately -- read once here, not
reimplemented per caller.

Token is looked for, in order:
  1. the NPM_TOKEN environment variable
  2. .env.local (repo root)
  3. .env (repo root)

npm rate-limits anonymous registry API callers more aggressively than
authenticated ones (confirmed via npm's own blog post on rolling out API
rate limiting, and directly here -- fetch_mcp_rankings.py's downloads
phase hit repeated 429s from registry.npmjs.org's search endpoint at
default_limiter()'s already-conservative 10/s+100/min pacing). A
**read-only** token is all this pipeline ever needs -- it only ever does
GET (search, package metadata), never publish -- so generate one at
npmjs.com -> Access Tokens -> Generate New Token -> Classic Token ->
"Read-only" (or `npm token create --read-only` from an already-logged-in
CLI), not an Automation/Publish-scoped one.

Every function here degrades to unauthenticated behavior if no token is
found anywhere, rather than raising -- callers still work, just at
whatever the lower anonymous ceiling turns out to be.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_LOCAL_FILE = ROOT / ".env.local"
ENV_FILE = ROOT / ".env"

# registry.npmjs.org is the actual npm registry API (search, package
# metadata, publish) -- the one that recognizes this token. api.npmjs.org
# (the separate downloads-point-in-time analytics service used as
# fetch_mcp_rankings.py's fallback) is a different, unauthenticated public
# API with no token concept -- never attach this header there.
NPM_HOSTS = {"registry.npmjs.org"}


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


def load_npm_token() -> str | None:
    token = os.environ.get("NPM_TOKEN")
    if token:
        return token
    for path in (ENV_LOCAL_FILE, ENV_FILE):
        token = _read_env_file(path, "NPM_TOKEN")
        if token:
            return token
    return None


NPM_TOKEN = load_npm_token()


def auth_headers() -> dict[str, str]:
    """Bearer-token header for registry.npmjs.org requests. Empty dict if
    no token is configured."""
    return {"Authorization": f"Bearer {NPM_TOKEN}"} if NPM_TOKEN else {}


def whoami() -> str | None:
    """Query registry.npmjs.org/-/whoami (the same endpoint `npm whoami`
    uses) -- lets a caller confirm a configured token is actually valid
    before kicking off a long run, the npm equivalent of
    github_auth.rate_limit_status(). Returns the authenticated username, or
    None if no token is configured, the token is invalid/expired
    (confirmed: unauthenticated returns 401), or the request fails for any
    other reason."""
    if not NPM_TOKEN:
        return None
    req = urllib.request.Request(
        "https://registry.npmjs.org/-/whoami", headers=auth_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("username")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
