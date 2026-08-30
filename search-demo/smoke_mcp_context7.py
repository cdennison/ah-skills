#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["qdrant-client"]
# ///

"""Smoke test: MCP-server pipeline (v0.3) end to end for **@upstash/context7-mcp**.

Registry id ``github:upstash/context7`` -- a real npm package
(``deployment: hybrid``: npm package + remote URL https://mcp.context7.com/mcp).
Chosen over the Daytona MCP servers because those are git-installed Python
projects with no ``server.json``/``package.json``/PyPI entry, so OSV can't
scan them; context7 exercises the full enrichment chain.

There is NO threat/LLM scan for MCP -- that does not exist and is out of
scope. What v0.3 covers: discovery -> repo-scan merge -> rankings ->
**OSV security scan** -> index -> query, none of it through the Vettd app.

Preconditions (full from-scratch recreation in ../vettd-e2e/E2E_TEST_PLAN_03.md):
  * ``github:upstash/context7`` in ``mcp-repo-seeds/registry.json`` with a
    ``repo_scan`` source descriptor (from ``enrich_from_repo_scan.py --ids``),
    ranking data (``fetch_mcp_rankings.py --ids``), and an OSV scan
    (``fetch_mcp_security.py --ids``);
  * indexed into ``mcp_servers`` in ``$QDRANT_URL`` via
    ``mcp-search/index_qdrant.py --ids github:upstash/context7``.

The test:
  1. locate the context7 point; assert repo-scan fields merged
     (registry_type==npm, package_identifier, deployment==hybrid, transport),
     ranking merged (stars > 0), and OSV ran (security_source=="osv",
     security_vuln_count is an int -- 0 is a real "scanned, clean" result).
     The package is clean but 4 of its 8 direct deps aren't, so also assert
     security_direct_deps_with_vulns is non-empty and
     security_direct_deps_max_severity is a real label (Findings 7 & 8);
  2. ``POST /query {asset_type:"mcp"}`` -- assert the hit surfaces all of the
     above, including the dep-vuln count / names / severity (McpHit used to
     drop stars + security entirely, then carried only the package summary);
  3. push-down filters: registry_type=[npm] keeps it / [pypi] drops it;
     deployment=[hybrid] keeps it / [local] drops it; min_stars below/above.

If nothing serves ``/query`` a throwaway ``uvicorn query_service:app`` is
spawned from ``app/`` and torn down after.

Run:  uv run python smoke_mcp_context7.py
Env:  QDRANT_URL         (default http://localhost:6350 -- the isolated store)
      SCAN_SERVICE_URL   (optional; used as-is if it already serves /query)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
COLLECTION = "mcp_servers"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6350")

TARGET_ID = "github:upstash/context7"
QUERY_TEXT = "context7 up to date library documentation"


def locate(client: QdrantClient) -> dict[str, Any]:
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, with_payload=True, with_vectors=False, limit=512, offset=offset,
        )
        for point in points:
            pl = point.payload or {}
            if pl.get("mcp_id") == TARGET_ID:
                return {"point_id": str(point.id), **pl}
        if offset is None:
            break
    raise SystemExit(
        f"{TARGET_ID} not in {COLLECTION} at {QDRANT_URL}.\n"
        "Recreate first -- see ../vettd-e2e/E2E_TEST_PLAN_03.md section 'A. Recreate'."
    )


def assert_payload(pl: dict[str, Any]) -> None:
    # repo-scan merge (Issue 1) -- these were all null before enrich_from_repo_scan.py
    if pl.get("registry_type") != "npm":
        raise SystemExit(f"FAIL: registry_type={pl.get('registry_type')!r}, expected 'npm' (repo-scan merge missing)")
    if not pl.get("package_identifier"):
        raise SystemExit("FAIL: no package_identifier (repo-scan merge missing)")
    if pl.get("deployment") != "hybrid":
        raise SystemExit(f"FAIL: deployment={pl.get('deployment')!r}, expected 'hybrid'")
    if not pl.get("transport"):
        raise SystemExit("FAIL: no transport (repo-scan merge missing)")
    # ranking merge (Issue 4)
    if not isinstance(pl.get("stars"), int) or pl["stars"] <= 0:
        raise SystemExit(f"FAIL: stars={pl.get('stars')!r}, expected a positive int")
    # OSV scan (Issue 3) -- vuln_count 0 is a real "scanned, nothing known", not a skip
    if pl.get("security_source") != "osv":
        raise SystemExit(f"FAIL: security_source={pl.get('security_source')!r}, expected 'osv' (OSV scan never ran)")
    if not isinstance(pl.get("security_vuln_count"), int):
        raise SystemExit(f"FAIL: security_vuln_count={pl.get('security_vuln_count')!r}, expected an int")
    # dependency-vuln signal (Findings 7 & 8) -- context7's package is clean,
    # so the whole security story is in the deps: undici/express advisories
    # are HIGH+, so max_severity must be a real label, not null.
    if not pl.get("security_direct_deps_with_vulns"):
        raise SystemExit(
            f"FAIL: security_direct_deps_with_vulns={pl.get('security_direct_deps_with_vulns')!r}, "
            "expected a non-empty list (context7 has 44 advisories across 4 direct deps)"
        )
    if pl.get("security_direct_deps_max_severity") is None:
        raise SystemExit(
            "FAIL: security_direct_deps_max_severity is None despite flagged deps "
            "(Finding 8 -- the dep pass must aggregate severity, not just counts)"
        )
    print(f"  {TARGET_ID}")
    print(f"    registry_type={pl['registry_type']}  package={pl['package_identifier']}  "
          f"deployment={pl['deployment']}  transport={pl['transport']}")
    print(f"    stars={pl['stars']}  weekly_downloads={pl.get('weekly_downloads')}  language={pl.get('language')}")
    print(f"    OSV: source={pl['security_source']}  vuln_count={pl['security_vuln_count']}  "
          f"max_severity={pl.get('security_max_severity')}  "
          f"direct_deps_scanned={pl.get('security_direct_deps_scanned')}  "
          f"direct_dep_vulns={pl.get('security_direct_deps_vuln_count')} "
          f"in {pl.get('security_direct_deps_with_vulns')}  "
          f"direct_deps_max_severity={pl.get('security_direct_deps_max_severity')}  "
          f"dep_vuln_ids={len(pl.get('security_direct_deps_vuln_ids') or [])}")


# ── query service (reuse or spawn) ────────────────────────────────────────

def _get(url: str, timeout: float = 5) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _serves(base_url: str, path: str) -> bool:
    try:
        return path in _get(f"{base_url}/openapi.json").get("paths", {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class QueryService:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self.base_url = ""

    def __enter__(self) -> "QueryService":
        configured = os.environ.get("SCAN_SERVICE_URL")
        if configured and _serves(configured.rstrip("/"), "/query"):
            self.base_url = configured.rstrip("/")
            print(f"  using existing query service at {self.base_url}")
            return self
        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        env = {**os.environ, "SKILLS_QDRANT_URL": QDRANT_URL, "MCP_QDRANT_URL": QDRANT_URL}
        for k in ("SKILLS_QDRANT_DB_PATH", "MCP_QDRANT_DB_PATH"):
            env.pop(k, None)
        print(f"  spawning query service: uvicorn query_service:app :{port}  (QDRANT_URL={QDRANT_URL})")
        self._proc = subprocess.Popen(
            [str(APP_DIR / ".venv" / "bin" / "uvicorn"), "query_service:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=APP_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if self._proc.poll() is not None:
                raise SystemExit("spawned query service exited during startup")
            try:
                _get(f"{self.base_url}/health", timeout=2)
                print("  query service is up")
                return self
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(0.5)
        raise SystemExit("spawned query service did not become ready")

    def __exit__(self, *_exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def post(self, path: str, body: dict[str, Any], timeout: float = 60) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"POST {path} -> {exc.code}: {exc.read().decode(errors='replace')[:400]}") from exc


def _ids(resp: dict[str, Any]) -> list[str]:
    return [h.get("mcp_id") for h in resp.get("hits", [])]


def main() -> int:
    print(f"[1/3] locating {TARGET_ID} in {COLLECTION} at {QDRANT_URL}")
    client = QdrantClient(url=QDRANT_URL, timeout=120)
    if not client.collection_exists(COLLECTION):
        raise SystemExit(f"collection {COLLECTION!r} does not exist at {QDRANT_URL}")
    assert_payload(locate(client))

    with QueryService() as svc:
        print(f"\n[2/3] POST /query asset_type=mcp {QUERY_TEXT!r} -- McpHit must surface the merged fields")
        resp = svc.post("/query", {"query": QUERY_TEXT, "asset_type": "mcp", "limit": 5})
        if TARGET_ID not in _ids(resp):
            raise SystemExit(f"FAIL: {TARGET_ID} not in /query hits: {_ids(resp)}")
        hit = next(h for h in resp["hits"] if h["mcp_id"] == TARGET_ID)
        for key, want in (("registry_type", "npm"), ("deployment", "hybrid"), ("security_source", "osv")):
            if hit.get(key) != want:
                raise SystemExit(f"FAIL: /query hit {key}={hit.get(key)!r}, expected {want!r} "
                                 f"(McpHit dropping the field?)")
        for key in ("package_identifier", "transport", "stars", "security_vuln_count"):
            if hit.get(key) in (None, ""):
                raise SystemExit(f"FAIL: /query hit missing {key!r} (McpHit dropping the field?)")
        # Findings 7 & 8: the dependency-vuln detail must ride the hit too --
        # for context7 the package is clean, so security_vuln_count=0 alone
        # would tell a query client "nothing to see here."
        if not hit.get("security_direct_deps_vuln_count"):
            raise SystemExit(
                f"FAIL: /query hit security_direct_deps_vuln_count={hit.get('security_direct_deps_vuln_count')!r} "
                "(Finding 7 -- McpHit must carry the dep-vuln count)"
            )
        if not hit.get("security_direct_deps_with_vulns"):
            raise SystemExit("FAIL: /query hit missing security_direct_deps_with_vulns (Finding 7)")
        if hit.get("security_direct_deps_max_severity") is None:
            raise SystemExit("FAIL: /query hit security_direct_deps_max_severity is None (Finding 8)")
        print(f"      hit: stars={hit['stars']} downloads/wk={hit.get('weekly_downloads')} "
              f"deployment={hit['deployment']} transport={hit['transport']} "
              f"OSV={hit['security_source']}/{hit['security_vuln_count']} own vulns, "
              f"{hit['security_direct_deps_vuln_count']} across deps "
              f"{hit['security_direct_deps_with_vulns']} (max {hit['security_direct_deps_max_severity']})")

        print("\n[3/3] push-down filters")
        checks = [
            ('registry_type=[npm]',  {"registry_type": ["npm"]},  True),
            ('registry_type=[pypi]', {"registry_type": ["pypi"]}, False),
            ('deployment=[hybrid]',  {"deployment": ["hybrid"]},  True),
            ('deployment=[local]',   {"deployment": ["local"]},   False),
            ('min_stars=1000',       {"min_stars": 1000},          True),
            ('min_stars=10_000_000', {"min_stars": 10_000_000},    False),
        ]
        for label, extra, expect_present in checks:
            got = _ids(svc.post("/query", {"query": "context7", "asset_type": "mcp", "limit": 5, **extra}))
            present = TARGET_ID in got
            mark = "ok" if present == expect_present else "FAIL"
            print(f"      {label:22} -> {'present' if present else 'absent':7} [{mark}]")
            if present != expect_present:
                raise SystemExit(f"FAIL: filter {label} -- expected present={expect_present}, got {present}")

    print("\nPASS - context7 MCP server: repo-scan merged, ranked, OSV-scanned, indexed, queryable,")
    print("       and every merged field surfaces through /query {asset_type:\"mcp\"} + push-down filters.")
    print("Note: no MCP threat/LLM scan and no Vettd surface - by design, see E2E_TEST_PLAN_03.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
