#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["qdrant-client"]
# ///

"""End-to-end smoke test for the proposed LLM-scan step (docs/ARCHITECTURE_LLM_SCAN.md).

One real skill, one real round trip:

  1. Query Qdrant (`agent_skills`) for a single high-star skill that already
     has a Vettd deterministic-scan *security* finding
     (`locations[].vettd_scan_findings.categories_flagged` contains "security").
     Skills targeting openclaw / hermes are excluded (by agent_compatibility,
     name, path, or owner).
  2. Scan that skill's SKILL.md text through the FastAPI `POST /scan` endpoint
     (non-deterministic, litellm/OpenRouter). If no running service exposes
     `/scan`, one is spawned from `app/` on an ephemeral port for the test and
     torn down afterwards.
  3. Write the verdict back onto that Qdrant point as a top-level `llm_scan`
     payload field (partial `set_payload`, exactly as the real step would),
     then read the point back and assert the field is present and well-formed.

This performs a real write to the live `agent_skills` collection. The point id
and a one-liner to remove the field again are printed at the end.

Run:  uv run python smoke_scan_top_skills.py
Env:  QDRANT_URL              (default http://localhost:6333)
      SCAN_SERVICE_URL        (optional; if it already serves /scan it is used
                               as-is, otherwise a temporary service is spawned)
      OPENROUTER_API_KEY / SKILL_SCANNER_LLM_API_KEY
                              (falls back to ../skill-scan-eval/.env)
      SKILL_SCANNER_LLM_MODEL (optional model override)
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
COLLECTION = "agent_skills"
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EXCLUDE_AGENTS = ("openclaw", "hermes")
PROMPT_PATH = APP_DIR / "prompts" / "skill_threat_analysis_prompt.md"
_SEVERITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


# ── step 1: pick the target skill ────────────────────────────────────────────

def _excluded(payload: dict[str, Any], location: dict[str, Any]) -> bool:
    blob = " ".join(
        str(v).lower()
        for v in (
            *(payload.get("agent_compatibility") or []),
            payload.get("name"),
            location.get("path"),
            location.get("owner"),
        )
        if v
    )
    return any(bad in blob for bad in EXCLUDE_AGENTS)


def find_target(client: QdrantClient) -> dict[str, Any]:
    """Highest-star skill point with a Vettd security finding, deterministic."""
    best: dict[str, Any] | None = None
    offset = None
    scanned = 0
    while True:
        points, offset = client.scroll(
            COLLECTION,
            with_payload=["name", "content", "content_hash", "locations", "agent_compatibility", "llm_scan"],
            with_vectors=False,
            limit=512,
            offset=offset,
        )
        for point in points:
            payload = point.payload or {}
            for location in payload.get("locations") or []:
                findings = location.get("vettd_scan_findings")
                if not findings or "security" not in (findings.get("categories_flagged") or []):
                    continue
                if _excluded(payload, location):
                    continue
                scanned += 1
                stars = location.get("stars") or 0
                # tie-break on point id so the pick never depends on scroll order
                key = (stars, str(point.id))
                if best is None or key > best["_key"]:
                    best = {
                        "_key": key,
                        "point_id": str(point.id),
                        "name": payload.get("name") or "",
                        "content": payload.get("content") or "",
                        "content_hash": payload.get("content_hash"),
                        "path": location.get("path"),
                        "stars": stars,
                        "existing_llm_scan": payload.get("llm_scan"),
                        "vettd_finding": findings,
                    }
        if offset is None:
            break
    if best is None:
        raise SystemExit("no skill with a Vettd security finding found (excluding openclaw/hermes)")
    best.pop("_key")
    print(f"  candidates with a security finding: {scanned}")
    return best


# ── step 2: the /scan endpoint ───────────────────────────────────────────────

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


def _serves_scan(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/openapi.json", timeout=5) as resp:
            spec = json.load(resp)
        return "/scan" in spec.get("paths", {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ScanService:
    """Use an existing /scan service if one is reachable, else spawn one."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._proc: subprocess.Popen[bytes] | None = None
        self.base_url = ""

    def __enter__(self) -> "ScanService":
        configured = os.environ.get("SCAN_SERVICE_URL")
        if configured and _serves_scan(configured.rstrip("/")):
            self.base_url = configured.rstrip("/")
            print(f"  using existing scan service at {self.base_url}")
            return self
        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "OPENROUTER_API_KEY": self._api_key,
            "SKILLS_QDRANT_URL": QDRANT_URL,
        }
        env.pop("SKILLS_QDRANT_DB_PATH", None)
        print(f"  spawning scan service: uvicorn query_service:app :{port} (cwd={APP_DIR})")
        self._proc = subprocess.Popen(
            [str(APP_DIR / ".venv" / "bin" / "uvicorn"), "query_service:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=APP_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if self._proc.poll() is not None:
                raise SystemExit("spawned scan service exited during startup")
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=2):
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

    def scan(self, skill_text: str, skill_name: str) -> dict[str, Any]:
        body = json.dumps({"skill_text": skill_text, "skill_name": skill_name}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/scan", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"/scan returned {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc


# ── step 3: assemble + persist the llm_scan payload field ────────────────────

def _max_severity(findings: list[dict[str, Any]]) -> str:
    ranks = [_SEVERITY_ORDER.index(f["severity"]) for f in findings if f.get("severity") in _SEVERITY_ORDER]
    return _SEVERITY_ORDER[max(ranks)] if ranks else "NONE"


def build_llm_scan(verdict: dict[str, Any], skill_text: str) -> dict[str, Any]:
    findings = verdict.get("findings") or []
    prompt_version = (
        hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12] if PROMPT_PATH.is_file() else "unknown"
    )
    return {
        "model": verdict.get("model", ""),
        "prompt_version": prompt_version,
        "scanned_at": datetime.now(UTC).isoformat(),
        "content_sha256": hashlib.sha256(skill_text.encode()).hexdigest(),
        "max_severity": _max_severity(findings),
        "finding_count": len(findings),
        "primary_threats": verdict.get("primary_threats") or [],
        "overall_assessment": verdict.get("overall_assessment", ""),
        "findings": findings,
    }


def main() -> int:
    print(f"[1/4] querying {COLLECTION} at {QDRANT_URL} for a skill with a Vettd security finding")
    client = QdrantClient(url=QDRANT_URL, timeout=120)
    if not client.collection_exists(COLLECTION):
        raise SystemExit(f"collection {COLLECTION!r} does not exist")
    target = find_target(client)
    print(f"      -> {target['name']!r}  ({target['path']}, {target['stars']} stars)")
    print(f"         point_id={target['point_id']}")
    print(f"         vettd finding: {json.dumps(target['vettd_finding'].get('top_findings'))}")
    if target["existing_llm_scan"] is not None:
        print("      note: this point already has an llm_scan field; it will be overwritten")
    if not target["content"].strip():
        raise SystemExit("target point has no `content` to scan")

    print("[2/4] scanning that skill via the /scan API (non-deterministic, ~20-40s)")
    api_key = _load_key()
    started = time.time()
    with ScanService(api_key) as service:
        verdict = service.scan(target["content"], target["name"])
    print(f"      -> model={verdict.get('model')}  findings={len(verdict.get('findings') or [])}"
          f"  threats={verdict.get('primary_threats')}  ({time.time() - started:.0f}s)")

    print("[3/4] writing the verdict to the Qdrant point as `llm_scan`")
    llm_scan = build_llm_scan(verdict, target["content"])
    client.set_payload(COLLECTION, payload={"llm_scan": llm_scan}, points=[target["point_id"]])
    print(f"      -> set_payload ok  (max_severity={llm_scan['max_severity']},"
          f" finding_count={llm_scan['finding_count']})")

    print("[4/4] reading the point back and verifying")
    (stored,) = client.retrieve(COLLECTION, ids=[target["point_id"]], with_payload=["llm_scan", "name"])
    got = (stored.payload or {}).get("llm_scan")
    required = {"model", "prompt_version", "scanned_at", "content_sha256",
               "max_severity", "finding_count", "primary_threats", "overall_assessment", "findings"}
    missing = required - set(got or {})
    if not got or missing:
        raise SystemExit(f"FAIL: llm_scan missing or incomplete (missing keys: {sorted(missing)})")
    if got["content_sha256"] != llm_scan["content_sha256"] or got["finding_count"] != llm_scan["finding_count"]:
        raise SystemExit("FAIL: stored llm_scan does not match what was written")
    print("      stored llm_scan:")
    print(json.dumps({k: got[k] for k in ("model", "prompt_version", "scanned_at",
                                          "content_sha256", "max_severity", "finding_count",
                                          "primary_threats")}, indent=2))

    print("\nPASS — scanned a real skill and persisted the verdict to Qdrant.")
    print(f"  point_id: {target['point_id']}  ({target['name']})")
    print("  remove the test write again with:")
    print(f'    uv run --with qdrant-client python3 -c \'from qdrant_client import QdrantClient; '
          f'QdrantClient(url="{QDRANT_URL}").delete_payload("{COLLECTION}", keys=["llm_scan"], '
          f'points=["{target["point_id"]}"])\'')
    return 0


if __name__ == "__main__":
    sys.exit(main())
