"""Shared rate-limited HTTP GET, for scripts hitting a public API/raw-content
host with no established auth or rate-limit guidance -- extracted because
mcp-search's pull_official_registry.py, pull_glama.py, pull_seed_repo.py, and
download_readmes.py all need the identical "pace it, retry transient errors,
treat 404 as a normal absent-file result" shape.

DEFAULT_LIMITS is the conservative cap given for this whole pipeline: no more
than 10 requests/sec, 100/min, AND 10000/hr, all simultaneously (the 100/min
ceiling is the one that actually binds -- 100/min is 6000/hr, tighter than
the 10000/hr cap on its own). Every new external source this pipeline hits
should share one RateLimiter instance per script run rather than constructing
a fresh one per call site, so the caps are enforced across the whole run, not
reset per-function.
"""

import sys
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from shared.github_auth import GITHUB_HOSTS, auth_headers
from shared.rate_limit import RateLimiter

USER_AGENT = "ah-skills-mcp-search/0.1 (+https://github.com/cdennison/ah-skills; contact: dennison.ch@gmail.com)"

DEFAULT_LIMITS = {1: 10, 60: 100, 3600: 10000}

# GitHub-specific ceiling, shared across every operation that hits a GitHub
# host within one script run (raw content, api.github.com, and shallow
# clones all draw from the same 4000/hr budget -- pass one RateLimiter
# instance into every tier, not a fresh one per call site, so the cap is
# enforced against the combined total). The 10/s entry is just a burst
# guard -- GitHub's secondary abuse-detection limiter reacts to burstiness
# even under an hourly quota that's technically fine -- the 4000/hr is the
# ceiling that actually matters here, and is deliberately kept below
# GitHub's own 5000/hr authenticated quota so we're never the ones tripping
# it.
GITHUB_LIMITS = {1: 10, 3600: 4000}

# A 429 from GitHub is not an ordinary transient failure to retry with a
# few seconds of backoff -- it means we've already been rate limited, and a
# short retry would just get 429'd again (or escalate into a longer block).
# Sleep well past a typical one-hour window before trying again, and don't
# count these against max_retries -- getting rate limited isn't "this
# request failed," it's "wait," so it shouldn't burn down the failure
# budget meant for genuine errors.
RATE_LIMIT_SLEEP_SECONDS = 70 * 60


def default_limiter() -> RateLimiter:
    return RateLimiter(DEFAULT_LIMITS)


def github_limiter() -> RateLimiter:
    return RateLimiter(GITHUB_LIMITS)


def _request(url: str, limiter: RateLimiter, *, data: bytes | None, extra_headers: dict,
             max_retries: int, timeout: float) -> bytes:
    """Shared retry/pacing/auth core for get()/post_json() -- a GET has no
    body (data=None, urllib defaults to GET), a POST passes `data` (urllib
    switches to POST automatically once a body is present). See get()'s
    docstring for the retry/rate-limit semantics; this is that logic,
    extracted once POST needed to share it rather than duplicate it."""
    headers = {"User-Agent": USER_AGENT, **extra_headers}
    if urllib.parse.urlsplit(url).netloc in GITHUB_HOSTS:
        headers.update(auth_headers())

    attempts = 0
    while True:
        limiter.wait()
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # GitHub's primary rate limit (unlike most APIs) responds 403,
            # not 429, once the core quota hits zero -- confirmed live: a
            # fetch_mcp_rankings.py run against api.github.com exhausted its
            # quota mid-run and this function, only special-casing 429,
            # treated every subsequent 403 as a normal (non-retryable)
            # error, burning through ~2600 doomed requests with zero
            # backoff before the caller's own error-recording loop finally
            # ran out of candidates. A 403 with `X-RateLimit-Remaining: 0`
            # is unambiguously that case (see
            # docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api)
            # -- sleep and retry exactly like 429. Any other 403 (a private
            # or blocked repo, e.g.) has no such header and is re-raised
            # immediately as before, since sleeping on a permissions error
            # would just waste an hour to fail the same way again.
            is_github_quota_403 = e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0"
            if e.code == 429 or is_github_quota_403:
                print(
                    f"[rate-limit] {e.code} from {url} -- sleeping {RATE_LIMIT_SLEEP_SECONDS // 60} min before retrying",
                    file=sys.stderr,
                )
                time.sleep(RATE_LIMIT_SLEEP_SECONDS)
                continue
            if 500 <= e.code < 600:
                attempts += 1
                if attempts >= max_retries:
                    raise
                time.sleep(min(2**attempts * 2, 30))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            attempts += 1
            if attempts >= max_retries:
                raise
            time.sleep(min(2**attempts * 2, 30))


def get(url: str, limiter: RateLimiter, max_retries: int = 4, timeout: float = 30.0) -> bytes:
    """Rate-limited GET returning raw bytes. A 429 (or a GitHub-quota-
    exhausted 403, see _request()) sleeps RATE_LIMIT_SLEEP_SECONDS and
    retries; 5xx/connection errors get a short exponential backoff and
    count against max_retries; anything else (including 404) is re-raised
    immediately for the caller to handle.

    Requests to a GitHub host (raw.githubusercontent.com, api.github.com,
    codeload.github.com) automatically carry the GITHUB_PAT bearer token
    when one is configured (see shared/github_auth.py) -- every other host
    (glama.ai, registry.modelcontextprotocol.io, api.osv.dev) is left
    unauthenticated, since a GitHub token has no business being sent there."""
    return _request(url, limiter, data=None, extra_headers={}, max_retries=max_retries, timeout=timeout)


def post_json(url: str, limiter: RateLimiter, payload: dict, max_retries: int = 4, timeout: float = 30.0) -> dict:
    """Rate-limited POST with a JSON body, JSON response parsed -- for APIs
    like OSV.dev's /v1/query that take the query as a POST body rather than
    query-string params. Same retry/rate-limit/GitHub-auth semantics as
    get() (via the shared _request() core); a GitHub host is never expected
    here in practice, but the same host-check exists for consistency, not
    because it's needed."""
    body = json.dumps(payload).encode("utf-8")
    raw = _request(
        url, limiter, data=body, extra_headers={"Content-Type": "application/json"},
        max_retries=max_retries, timeout=timeout,
    )
    return json.loads(raw)


def get_json(url: str, limiter: RateLimiter, **kwargs) -> dict:
    return json.loads(get(url, limiter, **kwargs))


def get_text_or_none(url: str, limiter: RateLimiter, **kwargs) -> str | None:
    """Like get(), but a 404 -> None instead of raising -- for optional
    files (server.json/package.json/README.md existence probes) where "not
    found" is a normal, expected outcome, not an error to log."""
    try:
        return get(url, limiter, **kwargs).decode(errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
