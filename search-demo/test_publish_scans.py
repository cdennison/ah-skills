from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

import publish_scans


def _fake_vettd(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-vettd"
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a") as stream:
    stream.write(json.dumps({"args": sys.argv[1:], "home": os.environ.get("HOME")}) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("vettd 9.8.7")
    raise SystemExit(0)
if sys.argv[1:] == ["auth", "status", "--json"]:
    mode = os.environ.get("FAKE_AUTH_STATUS", "valid")
    status = {"configured": True, "api_key_set": True, "reachable": True,
              "account": {"email": "publisher@example.test"}, "endpoint": os.environ["VETTD_SCAN_ENDPOINT"]}
    if mode == "malformed": print("bad " + os.environ["VETTD_API_KEY"])
    elif mode == "false": status["reachable"] = False; print(json.dumps(status))
    elif mode == "unconfigured": status["configured"] = False; print(json.dumps(status))
    elif mode == "key-unset": status["api_key_set"] = False; print(json.dumps(status))
    elif mode == "missing-account": status.pop("account"); print(json.dumps(status))
    elif mode == "wrong-account": status["account"] = {"email": "other@example.test"}; print(json.dumps(status))
    elif mode == "wrong-endpoint": status["endpoint"] += "/wrong"; print(json.dumps(status))
    else: print(json.dumps(status))
    raise SystemExit(int(os.environ.get("FAKE_AUTH_STATUS_EXIT", "0")))
if sys.argv[1] == "auth":
    raise SystemExit(0)
if sys.argv[1:3] == ["scan", "folder"]:
    report = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
    mode = os.environ.get("FAKE_REPORT", "valid")
    if mode == "malformed":
        report.write_text("{}")
    else:
        report.write_text(json.dumps({
            "scanMeta": {"scanId": "scan-123", "scannerVersion": os.environ.get("FAKE_REPORT_VERSION", "9.8.7")},
            "skills": [{"name": "demo"}],
        }))
    raise SystemExit(int(os.environ.get("FAKE_SCAN_EXIT", "0")))
if sys.argv[1:3] == ["scan", "submit"]:
    mode = os.environ.get("FAKE_SUBMIT", "accepted")
    if mode == "accepted": print("Scan accepted: scan-123", file=sys.stderr)
    elif mode == "duplicate": print("Scan already submitted (duplicate).")
    elif mode == "misleading": print('{"ok":true}')
    raise SystemExit(int(os.environ.get("FAKE_SUBMIT_EXIT", "0")))
raise SystemExit(9)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _indexed_db(db_path: Path, relative_skill: str) -> None:
    client = QdrantClient(path=str(db_path))
    try:
        client.create_collection(
            "agent_skills", vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE)
        )
        client.upsert(
            "agent_skills",
            points=[
                models.PointStruct(
                    id=1,
                    vector=[1.0],
                    payload={
                        "locations": [
                            {"path": relative_skill, "owner": "acme", "repo": "skills"},
                            {"path": "other/repo/skill/SKILL.md", "owner": "other", "repo": "repo"},
                        ]
                    },
                )
            ],
        )
    finally:
        client.close()


def _env(tmp_path: Path, db_path: Path, executable: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home inherited"),
        "VETTD_CLI_BIN": str(executable),
        "VETTD_SCAN_ENDPOINT": "http://127.0.0.1:3000/api/scans/ingest",
        "VETTD_API_KEY": "super-secret-key",
        "VETTD_EXPECTED_ACCOUNT_EMAIL": "publisher@example.test",
        "SKILLS_QDRANT_DB_PATH": str(db_path),
        "FAKE_LOG": str(tmp_path / "vettd.jsonl"),
    }


def _prepare(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> publish_scans.PreparedPublisher:
    for key in (
        "VETTD_CLI_BIN",
        "VETTD_SCAN_ENDPOINT",
        "VETTD_API_KEY",
        "VETTD_EXPECTED_ACCOUNT_EMAIL",
        "SKILLS_QDRANT_DB_PATH",
        "SKILLS_QDRANT_URL",
        "HOME",
        "FAKE_LOG",
        "FAKE_REPORT",
        "FAKE_SUBMIT",
        "FAKE_REPORT_VERSION",
        "FAKE_AUTH_STATUS",
        "FAKE_AUTH_STATUS_EXIT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return publish_scans.preflight(publish_scans.PublishConfig.from_env())


def _cleanup_skill_fixture(base: Path) -> None:
    run_root = base.parent
    for child in sorted(base.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink(missing_ok=True)
    base.rmdir()
    if run_root.is_dir() and not any(run_root.iterdir()):
        run_root.rmdir()


def test_preflight_does_not_create_missing_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, tmp_path / "empty-db", executable)

    # When / Then
    with pytest.raises(publish_scans.PreflightError):
        _prepare(monkeypatch, env)
    client = QdrantClient(path=str(tmp_path / "empty-db"))
    try:
        assert not client.collection_exists("agent_skills")
    finally:
        client.close()


def test_publish_records_receipt_and_rerun_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    skill_dir = (
        publish_scans.SEARCH_RAW / "publisher-tests" / tmp_path.parent.name / tmp_path.name / "demo"
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# Demo\n", encoding="utf-8")
    (skill_dir / "reference.txt").write_text("content", encoding="utf-8")
    relative_skill = (skill_dir / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()
    db_path = tmp_path / "qdrant"
    _indexed_db(db_path, relative_skill)
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, db_path, executable)
    Path(env["HOME"]).mkdir()
    prepared = _prepare(monkeypatch, env)

    try:
        # When
        first = publish_scans.publish_skill_directories([skill_dir], prepared)
        second = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert (first.attempted, first.succeeded, first.skipped, first.failed) == (1, 1, 0, 0)
        assert (second.attempted, second.succeeded, second.skipped, second.failed) == (1, 0, 1, 0)
        points, _ = prepared.client.scroll("agent_skills", with_payload=True)
        payload = points[0].payload
        assert payload is not None
        locations = payload["locations"]
        receipt = locations[0]["vettd_scan_publications"][0]
        assert receipt["endpoint"] == env["VETTD_SCAN_ENDPOINT"]
        assert receipt["scanner_version"] == "9.8.7"
        assert receipt["scan_id"] == "scan-123"
        assert receipt["status"] == "accepted"
        assert receipt["target_fingerprint"] != env["VETTD_API_KEY"]
        assert "vettd_scan_publications" not in locations[1]
        invocations = [json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()]
        assert sum(row["args"][:2] == ["scan", "folder"] for row in invocations) == 1
        assert next(row for row in invocations if row["args"][0] == "auth")["home"] == env["HOME"]
        assert any(row["args"] == ["auth", "status", "--json"] for row in invocations)
        assert all(env["VETTD_API_KEY"] not in json.dumps(row) or row["args"][0] == "auth" for row in invocations)
    finally:
        prepared.client.close()
        _cleanup_skill_fixture(skill_dir.parent)
        assert not skill_dir.parents[1].exists()


def test_publish_continues_after_misleading_submit_and_cleans_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    base = publish_scans.SEARCH_RAW / "publisher-tests" / tmp_path.parent.name / tmp_path.name
    first, second = base / "first", base / "second"
    for skill_dir in (first, second):
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill", encoding="utf-8")
    relative = (first / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()
    db_path = tmp_path / "qdrant"
    _indexed_db(db_path, relative)
    client = QdrantClient(path=str(db_path))
    try:
        client.upsert(
            "agent_skills",
            points=[models.PointStruct(id=2, vector=[1.0], payload={"locations": [{"path": (second / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()}]})],
        )
    finally:
        client.close()
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, db_path, executable) | {"FAKE_SUBMIT": "misleading"}
    Path(env["HOME"]).mkdir()
    prepared = _prepare(monkeypatch, env)

    try:
        # When
        summary = publish_scans.publish_skill_directories([first, second], prepared)

        # Then
        assert (summary.attempted, summary.succeeded, summary.skipped, summary.failed) == (2, 0, 0, 2)
        assert len(summary.failures) == 2
        invocations = [json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()]
        reports = [Path(row["args"][2]) for row in invocations if row["args"][:2] == ["scan", "submit"]]
        assert len(reports) == 2
        assert all(not report.exists() for report in reports)
        assert env["VETTD_API_KEY"] not in " ".join(failure.message for failure in summary.failures)
    finally:
        prepared.client.close()
        _cleanup_skill_fixture(base)
        assert not base.parent.exists()


def test_unindexed_directory_is_skipped_without_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    skill_dir = (
        publish_scans.SEARCH_RAW
        / "publisher-tests"
        / tmp_path.parent.name
        / tmp_path.name
        / "unindexed"
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# unindexed", encoding="utf-8")
    db_path = tmp_path / "qdrant"
    _indexed_db(db_path, "different/SKILL.md")
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, db_path, executable)
    Path(env["HOME"]).mkdir()
    prepared = _prepare(monkeypatch, env)

    try:
        # When
        summary = publish_scans.publish_skill_directories([skill_dir], prepared)

        # Then
        assert (summary.attempted, summary.succeeded, summary.skipped, summary.failed) == (1, 0, 1, 0)
        invocations = [json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()]
        assert not any(row["args"][:2] == ["scan", "folder"] for row in invocations)
    finally:
        prepared.client.close()
        _cleanup_skill_fixture(skill_dir.parent)
        assert not skill_dir.parents[1].exists()


def test_cli_returns_preflight_exit_two_without_secret(tmp_path: Path) -> None:
    # Given
    env = os.environ | {
        "VETTD_API_KEY": "never-print-this-key",
        "VETTD_SCAN_ENDPOINT": "https://example.test/wrong",
    }

    # When
    completed = subprocess.run(
        [sys.executable, str(Path(publish_scans.__file__)), str(tmp_path)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    # Then
    assert completed.returncode == 2
    assert "never-print-this-key" not in completed.stdout + completed.stderr
