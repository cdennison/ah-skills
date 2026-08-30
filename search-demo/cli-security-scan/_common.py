"""Shared paths + a cached, backoff-retrying JSON fetch helper for the
CLI-security pipeline.

Every response (npm registry, PyPI, OSV.dev) is cached on disk under
work/cache/ keyed by (kind, ecosystem, package), so a re-run is network-free
and a mid-run failure never loses progress. Cache entries record the fetch
date; the README's refresh instructions are just "delete the relevant
work/cache/ subtree and re-run".
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SEARCH_RAW = ROOT / "search-raw"
WORK = HERE / "work"
CACHE = WORK / "cache"

WORK.mkdir(exist_ok=True)

ECOSYSTEMS = ("npm", "pip")

# Ordering used everywhere a "worst severity" is picked. "" / unknown sorts
# to 0 so callers can decide how to treat an advisory OSV gave no label for
# (build_cli_export.grade_for_package treats it conservatively as the worst
# grade -- see the design doc's "grade over-states risk" section).
SEVERITY_ORDER = {"": 0, "NONE": 0, "LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

_RETRY_STATUS = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 3

_USER_AGENT = "ah-skills-cli-security-scan (+https://github.com/anthropics/ah-skills)"


def _safe_name(name: str) -> str:
    """Filesystem-safe cache filename for a package name (npm scopes have a
    '/', versions/extras can't appear here but be defensive)."""
    return name.replace("/", "__").replace("\\", "__").replace("..", "__") or "_"


def _cache_path(kind: str, ecosystem: str, package: str) -> Path:
    return CACHE / kind / ecosystem / f"{_safe_name(package)}.json"


def cached_json(
    kind: str,
    ecosystem: str,
    package: str,
    request_factory,
    *,
    refresh: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """Return the parsed JSON body for a request, from disk cache when present.

    `request_factory()` returns a urllib.request.Request (called only on a
    cache miss). The return value is always a dict: on success the response
    body (wrapped as {"__list__": [...]} if the API returned a bare list),
    otherwise {"__error__": "<reason>"}. Errors are cached too -- a 404 for a
    private/renamed package won't change on a re-run within the same refresh
    cycle -- but transient retry-class failures are not.
    """
    path = _cache_path(kind, ecosystem, package)
    if not refresh and path.exists():
        try:
            entry = json.loads(path.read_text())
            return entry["result"]
        except (json.JSONDecodeError, KeyError):
            pass  # corrupt cache entry -> refetch

    result = _fetch(request_factory(), timeout=timeout, max_retries=max_retries)
    time.sleep(0.05)  # light politeness on cache-miss traffic; re-runs are cache-served

    cacheable = "__error__" not in result or result["__error__"].startswith("HTTP ")
    if cacheable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched": _dt.date.today().isoformat(),
            "kind": kind,
            "ecosystem": ecosystem,
            "package": package,
            "result": result,
        }))
    return result


def _fetch(req: urllib.request.Request, *, timeout: int, max_retries: int) -> dict:
    req.add_header("User-Agent", _USER_AGENT)
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            return {"__list__": data} if isinstance(data, list) else data
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_STATUS and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return {"__error__": f"HTTP {e.code}"}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return {"__error__": str(e)}
    return {"__error__": "retries exhausted"}


def osv_snapshot_date() -> str:
    """Oldest fetch date across the OSV cache -- the effective 'as of' date
    for a set of grades. Falls back to today when the cache is empty."""
    dates = [
        json.loads(p.read_text()).get("fetched")
        for p in (CACHE / "osv").rglob("*.json")
    ]
    dates = [d for d in dates if d]
    return min(dates) if dates else _dt.date.today().isoformat()
