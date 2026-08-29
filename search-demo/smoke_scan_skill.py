#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["qdrant-client"]
# ///

"""Smoke test: POST /scan/skill, then see the verdict via POST /query.

Hard-coded to one real indexed skill (``TARGET_SKILL_PATH``) that already
carries a **published Vettd deterministic scan** on its Qdrant point
(``locations[].vettd_scan_findings`` + ``vettd_scan_publications``). The test:

  1. locates that skill's point in ``agent_skills`` and confirms the Vettd
     scan data is there;
  2. calls ``POST /scan/skill {point_id, force}`` -- the endpoint scans the
     SKILL.md text (non-deterministic, litellm/OpenRouter) and writes a
     top-level ``llm_scan`` payload field itself (one real write; ~20-40s);
  3. calls ``POST /query`` for that skill and asserts the single response
     carries **both** scans: the deterministic Vettd findings (inside
     ``locations[]``) and the non-deterministic ``llm_scan``.

It prints copy-pasteable ``curl`` request/response for steps 2 and 3 -- the
worked example kept in ``docs/ARCHITECTURE_LLM_SCAN.md``.

If nothing serves ``/scan/skill`` a throwaway ``uvicorn query_service:app``
is spawned from ``app/`` for the test and torn down after.

Run:  uv run python smoke_scan_skill.py
Env:  QDRANT_URL         (default http://localhost:6333)
      SCAN_SERVICE_URL   (optional; used as-is if it already serves /scan/skill)
      OPENROUTER_API_KEY / SKILL_SCANNER_LLM_API_KEY
                         (falls back to ../skill-scan-eval/.env)
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
COLLECTION = "agent_skills"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

# A real indexed skill whose Qdrant point already has a published Vettd scan
# (deterministic: grade B, VTD-0088 "references external URL" security finding).
TARGET_SKILL_PATH = "steipete/clawdis/.agents/skills/crabbox/SKILL.md"
QUERY_TEXT = "crabbox"


# ── step 1: locate the hard-coded skill ─────────────────────────────────────

def locate(client: QdrantClient) -> dict[str, Any]:
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION,
            with_payload=["name", "content", "content_hash", "locations"],
            with_vectors=False,
            limit=512,
            offset=offset,
        )
        for point in points:
            payload = point.payload or {}
            for location in payload.get("locations") or []:
                if location.get("path") == TARGET_SKILL_PATH:
                    return {
                        "point_id": str(point.id),
                        "name": payload.get("name") or "",
                        "content": payload.get("content") or "",
                        "content_hash": payload.get("content_hash") or "",
                        "location": location,
                    }
        if offset is None:
            break
    raise SystemExit(f"skill {TARGET_SKILL_PATH!r} not found in {COLLECTION}")


def assert_vettd_scan(location: dict[str, Any]) -> None:
    findings = location.get("vettd_scan_findings")
    publications = location.get("vettd_scan_publications")
    if not findings or "security" not in (findings.get("categories_flagged") or []):
        raise SystemExit("target skill has no Vettd *security* finding -- pick another TARGET_SKILL_PATH")
    if not publications:
        raise SystemExit("target skill's Vettd scan was never published (no vettd_scan_publications)")
    print(f"  Vettd scan: grade {findings.get('overall_grade')},"
          f" {findings.get('finding_count')} findings, categories={findings.get('categories_flagged')}")
    print(f"  Vettd publication: scan_id={publications[0].get('scan_id')},"
          f" status={publications[0].get('status')}, scanner {publications[0].get('scanner_version')}")


# ── the scan service (reuse or spawn) ──────────────────────────────────────

def _load_key() -> str:
    key = os.environ.get("SKILL_SCANNER_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    fallback = ROOT.parent / "skill-scan-eval" / ".env"
    if fallback.is_file():
        for line in fallback.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    raise SystemExit("no OPENROUTER_API_KEY / SKILL_SCANNER_LLM_API_KEY available")


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


class ScanService:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._proc: subprocess.Popen[bytes] | None = None
        self.base_url = ""

    def __enter__(self) -> ScanService:
        configured = os.environ.get("SCAN_SERVICE_URL")
        if configured and _serves(configured.rstrip("/"), "/scan/skill"):
            self.base_url = configured.rstrip("/")
            print(f"  using existing scan service at {self.base_url}")
            return self
        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        env = {**os.environ, "OPENROUTER_API_KEY": self._api_key, "SKILLS_QDRANT_URL": QDRANT_URL}
        env.pop("SKILLS_QDRANT_DB_PATH", None)
        print(f"  spawning scan service: uvicorn query_service:app :{port}")
        self._proc = subprocess.Popen(
            [str(APP_DIR / ".venv" / "bin" / "uvicorn"), "query_service:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=APP_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if self._proc.poll() is not None:
                raise SystemExit("spawned scan service exited during startup")
            try:
                _get(f"{self.base_url}/health", timeout=2)
                print("  scan service is up")
                return self
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(0.5)
        raise SystemExit("spawned scan service did not become ready")

    def __exit__(self, *_exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def post(self, path: str, body: dict[str, Any], timeout: float = 200) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"POST {path} -> {exc.code}: {exc.read().decode(errors='replace')[:400]}") from exc


# ── curl rendering for the worked example ──────────────────────────────────

def curl(path: str, body: dict[str, Any]) -> str:
    return (
        f"curl -sS -X POST http://localhost:8000{path} \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{json.dumps(body)}'"
    )


def _trim(hit: dict[str, Any]) -> dict[str, Any]:
    """A hit is large (SKILL.md content, every location). Keep the parts the
    example is about."""
    vettd_loc = next(
        (loc for loc in hit.get("locations", []) if loc.get("vettd_scan_findings")), {}
    )
    return {
        "name": hit.get("name"),
        "path": hit.get("path"),
        "stars": hit.get("stars"),
        "llm_scan": hit.get("llm_scan"),
        "locations[vettd]": {
            "path": vettd_loc.get("path"),
            "vettd_scan_findings": vettd_loc.get("vettd_scan_findings"),
            "vettd_scan_publications": vettd_loc.get("vettd_scan_publications"),
        },
    }


def main() -> int:
    print(f"[1/3] locating {TARGET_SKILL_PATH!r} in {COLLECTION} at {QDRANT_URL}")
    client = QdrantClient(url=QDRANT_URL, timeout=120)
    if not client.collection_exists(COLLECTION):
        raise SystemExit(f"collection {COLLECTION!r} does not exist")
    target = locate(client)
    print(f"      point_id={target['point_id']}  name={target['name']!r}")
    assert_vettd_scan(target["location"])
    if not target["content"].strip():
        raise SystemExit("target point has no `content` to scan")

    api_key = _load_key()
    with ScanService(api_key) as service:
        print("\n[2/3] POST /scan/skill  (endpoint scans + writes llm_scan; ~20-40s)")
        scan_req = {"point_id": target["point_id"], "force": True}
        print("      $ " + curl("/scan/skill", scan_req).replace("\n", "\n      "))
        started = time.time()
        scan_resp = service.post("/scan/skill", scan_req)
        print(f"      response ({time.time() - started:.0f}s):")
        print(json.dumps(scan_resp, indent=2))
        if scan_resp.get("skipped") is not False or "llm_scan" not in scan_resp:
            raise SystemExit("FAIL: /scan/skill did not return a fresh llm_scan verdict")
        llm = scan_resp["llm_scan"]
        for key in ("model", "prompt_version", "scanned_at", "content_sha256",
                    "max_severity", "finding_count", "primary_threats", "findings"):
            if key not in llm:
                raise SystemExit(f"FAIL: llm_scan missing key {key!r}")

        print(f"\n[3/3] POST /query {QUERY_TEXT!r} -- one response, both scans")
        query_req = {"query": QUERY_TEXT, "asset_type": "skill", "limit": 25}
        print("      $ " + curl("/query", query_req).replace("\n", "\n      "))
        query_resp = service.post("/query", query_req)

    hit = next(
        (h for h in query_resp.get("hits", [])
         if h.get("path") == TARGET_SKILL_PATH
         or any(loc.get("path") == TARGET_SKILL_PATH for loc in h.get("locations", []))),
        None,
    )
    if hit is None:
        raise SystemExit(f"FAIL: {TARGET_SKILL_PATH!r} not in /query hits for {QUERY_TEXT!r}")

    print("      response hit (trimmed to the relevant fields):")
    print(json.dumps(_trim(hit), indent=2))

    if not hit.get("llm_scan"):
        raise SystemExit("FAIL: /query hit has no llm_scan")
    vettd_loc = next((loc for loc in hit.get("locations", []) if loc.get("vettd_scan_findings")), None)
    if vettd_loc is None or not vettd_loc.get("vettd_scan_publications"):
        raise SystemExit("FAIL: /query hit has no published Vettd scan in its locations")
    if "security" not in (vettd_loc["vettd_scan_findings"].get("categories_flagged") or []):
        raise SystemExit("FAIL: /query hit's Vettd scan lost its security category")

    print("\nPASS — one /query response carried both:")
    print(f"  - Vettd (deterministic):  grade {vettd_loc['vettd_scan_findings'].get('overall_grade')},"
          f" scan_id {vettd_loc['vettd_scan_publications'][0].get('scan_id')}")
    print(f"  - llm_scan (non-deterministic):  model {hit['llm_scan'].get('model')},"
          f" {hit['llm_scan'].get('finding_count')} findings,"
          f" max_severity {hit['llm_scan'].get('max_severity')}")
    print(f"\n  point_id {target['point_id']}. Remove the llm_scan test write with:")
    print(f'    uv run --with qdrant-client python3 -c \'from qdrant_client import QdrantClient; '
          f'QdrantClient(url="{QDRANT_URL}").delete_payload("{COLLECTION}", keys=["llm_scan"], '
          f'points=["{target["point_id"]}"])\'')
    return 0


if __name__ == "__main__":
    sys.exit(main())
