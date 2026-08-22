from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from qdrant_client import QdrantClient

import publish_scans
import test_publish_scans as support


class _QdrantPublishHandler(BaseHTTPRequestHandler):
    locations: ClassVar[list[publish_scans.JsonObject]] = []
    requests: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path))
        self._respond(b'{"result":{"exists":true},"status":"ok","time":0}' if urlsplit(self.path).path.endswith("/collections/agent_skills/exists") else b'{"version":"1.19.0"}')

    def do_POST(self) -> None:
        self.requests.append(("POST", self.path))
        request_path = urlsplit(self.path).path
        if request_path.endswith("/points/scroll"):
            body = json.dumps(
                {
                    "result": {"points": [{"id": 1, "payload": {"locations": self.locations}}], "next_page_offset": None}, "status": "ok", "time": 0,
                }
            ).encode()
        elif request_path.endswith("/points/payload"):
            self.locations[:] = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))["payload"]["locations"]
            body = b'{"result":{"status":"completed"},"status":"ok","time":0}'
        else:
            body = b'{"status":{"error":"unexpected request"},"time":0}'
        self._respond(body)

    def _respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

def _skill_and_db(tmp_path: Path, filename: str = "SKILL.md") -> tuple[Path, Path]:
    skill_dir = publish_scans.SEARCH_RAW / "publisher-tests" / tmp_path.parent.name / tmp_path.name / "edge"
    skill_dir.mkdir(parents=True)
    (skill_dir / filename).write_text("# edge", encoding="utf-8")
    relative = (skill_dir / filename).relative_to(publish_scans.SEARCH_RAW).as_posix()
    db_path = tmp_path / "qdrant"
    support._indexed_db(db_path, relative)
    return skill_dir, db_path


def _cleanup(skill_dir: Path) -> None:
    support._cleanup_skill_fixture(skill_dir.parent)


def test_preflight_rejects_empty_scanner_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    executable = support._fake_vettd(tmp_path)
    executable.write_text(executable.read_text().replace('print("vettd 9.8.7")', "pass"))
    skill_dir, db_path = _skill_and_db(tmp_path)
    env = support._env(tmp_path, db_path, executable)
    Path(env["HOME"]).mkdir()

    # When / Then
    try:
        with pytest.raises(publish_scans.PreflightError):
            support._prepare(monkeypatch, env)
    finally:
        _cleanup(skill_dir)
        assert not skill_dir.parents[1].exists()


def test_config_rejects_userinfo_and_empty_url_suffixes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setenv("VETTD_API_KEY", "secret")
    monkeypatch.delenv("SKILLS_QDRANT_DB_PATH", raising=False)
    monkeypatch.delenv("SKILLS_QDRANT_URL", raising=False)

    # When / Then
    for endpoint in (
        "https://user@example.test/api/scans/ingest",
        "https://example.test/api/scans/ingest?",
        "https://example.test/api/scans/ingest#",
    ):
        monkeypatch.setenv("VETTD_SCAN_ENDPOINT", endpoint)
        with pytest.raises(publish_scans.ConfigurationError):
            publish_scans.PublishConfig.from_env()


@pytest.mark.parametrize("report_mode,scan_exit", [("malformed", "0"), ("valid", "7")])
def test_invalid_scan_never_writes_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_mode: str, scan_exit: str) -> None:
    # Given
    skill_dir, db_path = _skill_and_db(tmp_path)
    executable = support._fake_vettd(tmp_path)
    env = support._env(tmp_path, db_path, executable) | {
        "FAKE_REPORT": report_mode,
        "FAKE_SCAN_EXIT": scan_exit,
    }
    Path(env["HOME"]).mkdir()
    prepared = support._prepare(monkeypatch, env)

    try:
        # When
        summary = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert summary.failed == 1
        points, _ = prepared.client.scroll("agent_skills", with_payload=True)
        payload = points[0].payload
        assert payload is not None
        assert "vettd_scan_publications" not in payload["locations"][0]
    finally:
        prepared.client.close()
        _cleanup(skill_dir)


def test_duplicate_marker_records_duplicate_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    skill_dir, db_path = _skill_and_db(tmp_path)
    executable = support._fake_vettd(tmp_path)
    env = support._env(tmp_path, db_path, executable) | {"FAKE_SUBMIT": "duplicate"}
    Path(env["HOME"]).mkdir()
    prepared = support._prepare(monkeypatch, env)

    try:
        # When
        summary = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert summary.succeeded == 1
        points, _ = prepared.client.scroll("agent_skills", with_payload=True)
        payload = points[0].payload
        assert payload is not None
        assert payload["locations"][0]["vettd_scan_publications"][0]["status"] == "duplicate"
    finally:
        prepared.client.close()
        _cleanup(skill_dir)


def test_receipt_uses_preflight_scanner_version_when_report_disagrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    skill_dir, db_path = _skill_and_db(tmp_path)
    executable = support._fake_vettd(tmp_path)
    env = support._env(tmp_path, db_path, executable) | {"FAKE_REPORT_VERSION": "stale-report-version"}
    Path(env["HOME"]).mkdir()
    prepared = support._prepare(monkeypatch, env)

    try:
        # When
        first = publish_scans.publish_skill_directories([skill_dir], prepared)
        second = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert first.succeeded == 1
        assert second.skipped == 1
        points, _ = prepared.client.scroll("agent_skills", with_payload=True)
        payload = points[0].payload
        assert payload is not None
        receipt = payload["locations"][0]["vettd_scan_publications"][0]
        assert receipt["scanner_version"] == prepared.scanner_version
    finally:
        prepared.client.close()
        _cleanup(skill_dir)


def test_stale_receipts_do_not_skip_current_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    skill_dir, db_path = _skill_and_db(tmp_path)
    client = QdrantClient(path=str(db_path))
    try:
        points, _ = client.scroll("agent_skills", with_payload=True)
        payload = points[0].payload
        assert payload is not None
        location = payload["locations"][0]
        location["vettd_scan_publications"] = [
            {"target_fingerprint": "old-key", "content_sha256": "old-content", "scanner_version": "0.0.1"}
        ]
        _ = client.set_payload("agent_skills", {"locations": payload["locations"]}, points=[1])
    finally:
        client.close()
    executable = support._fake_vettd(tmp_path)
    env = support._env(tmp_path, db_path, executable)
    Path(env["HOME"]).mkdir()
    prepared = support._prepare(monkeypatch, env)

    try:
        # When
        summary = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert summary.succeeded == 1
    finally:
        prepared.client.close()
        _cleanup(skill_dir)


def test_git_metadata_does_not_change_folder_identity(tmp_path: Path) -> None:
    # Given
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# stable", encoding="utf-8")
    before = publish_scans._folder_hash(skill_dir)
    git_dir = skill_dir / ".git"
    git_dir.mkdir()
    (git_dir / "dirty").write_text("worktree noise", encoding="utf-8")

    # When
    after = publish_scans._folder_hash(skill_dir)

    # Then
    assert after == before


def test_ambiguous_case_insensitive_skill_filenames_fail_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    skill_dir, db_path = _skill_and_db(tmp_path)
    (skill_dir / "skill.MD").write_text("# ambiguous", encoding="utf-8")
    if len(tuple(skill_dir.iterdir())) == 1:
        original_iterdir = Path.iterdir

        def case_sensitive_listing(path: Path) -> Iterator[Path]:
            return iter((path / "SKILL.md", path / "skill.MD")) if path == skill_dir else original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", case_sensitive_listing)
    executable = support._fake_vettd(tmp_path)
    env = support._env(tmp_path, db_path, executable)
    Path(env["HOME"]).mkdir()
    prepared = support._prepare(monkeypatch, env)

    try:
        # When
        summary = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert summary.failed == 1
        assert "multiple case-insensitive SKILL.md files" in summary.failures[0].message
    finally:
        prepared.client.close()
        _cleanup(skill_dir)


def test_url_mode_publishes_indexed_skill_and_updates_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    skill_dir = publish_scans.SEARCH_RAW / "publisher-tests" / tmp_path.parent.name / tmp_path.name / "url-mode"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# URL mode", encoding="utf-8")
    relative_skill = (skill_dir / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()
    _QdrantPublishHandler.locations = [{"path": relative_skill, "owner": "acme"}]
    _QdrantPublishHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QdrantPublishHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    executable = support._fake_vettd(tmp_path)
    env = support._env(tmp_path, tmp_path / "unused", executable)
    env.pop("SKILLS_QDRANT_DB_PATH")
    env["SKILLS_QDRANT_URL"] = f"http://127.0.0.1:{server.server_port}"
    Path(env["HOME"]).mkdir()
    prepared: publish_scans.PreparedPublisher | None = None

    try:
        prepared = support._prepare(monkeypatch, env)

        # When
        summary = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert (summary.succeeded, summary.failed) == (1, 0), summary.failures
        assert any(urlsplit(path).path.endswith("/points/scroll") for method, path in _QdrantPublishHandler.requests if method == "POST")
        assert any(urlsplit(path).path.endswith("/points/payload") for method, path in _QdrantPublishHandler.requests if method == "POST")
        receipt_values = _QdrantPublishHandler.locations[0]["vettd_scan_publications"]
        assert isinstance(receipt_values, list)
        receipt = next(value for value in receipt_values if isinstance(value, dict))
        assert receipt["scan_id"] == "scan-123"
        assert not any("collections" in path and method in {"PUT", "POST"} and not urlsplit(path).path.endswith(("/points/scroll", "/points/payload")) for method, path in _QdrantPublishHandler.requests)
    finally:
        if prepared is not None:
            prepared.client.close()
        server.shutdown()
        thread.join()
        server.server_close()
        _cleanup(skill_dir)


def test_real_cli_publishes_then_skips_unchanged(tmp_path: Path) -> None:
    # Given
    skill_dir, db_path = _skill_and_db(tmp_path, "skill.MD")
    executable = support._fake_vettd(tmp_path)
    configured = support._env(tmp_path, db_path, executable)
    Path(configured["HOME"]).mkdir()
    environment = os.environ | configured
    environment.pop("SKILLS_QDRANT_URL", None)
    command = [sys.executable, str(Path(publish_scans.__file__)), str(skill_dir)]

    try:
        # When
        first = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
        second = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)

        # Then
        assert first.returncode == 0
        assert "succeeded=1" in first.stdout
        assert second.returncode == 0
        assert "skipped=1" in second.stdout
        client = QdrantClient(path=str(db_path))
        try:
            points, _ = client.scroll("agent_skills", with_payload=True)
            payload = points[0].payload
            assert payload is not None
            assert payload["locations"][0]["vettd_scan_publications"][0]["scan_id"] == "scan-123"
        finally:
            client.close()
        invocations = [json.loads(line) for line in Path(configured["FAKE_LOG"]).read_text().splitlines()]
        assert sum(row["args"][:2] == ["scan", "folder"] for row in invocations) == 1
    finally:
        _cleanup(skill_dir)
