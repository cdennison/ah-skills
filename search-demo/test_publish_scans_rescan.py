"""Rescan-interval gating (docs/ARCHITECTURE_PUBLISHING_SCANS.md): no prior
receipt -> always rescan; within the interval -> skip without hashing;
past the interval -> hash-gated (skip if unchanged, rescan if changed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

import publish_scans
from test_publish_scans import _env, _fake_vettd, _indexed_db, _prepare


def _scan_folder_invocations(fake_log: Path) -> int:
    if not fake_log.exists():
        return 0
    return sum(
        json.loads(line)["args"][:2] == ["scan", "folder"]
        for line in fake_log.read_text().splitlines()
    )


def _backdate_receipt(client: QdrantClient, relative_skill: str, *, days: int) -> None:
    # Embedded Qdrant takes an exclusive file lock, so this must reuse the
    # already-open client (prepared.client) rather than opening a second one
    # against the same on-disk path.
    points, _ = client.scroll("agent_skills", with_payload=True)
    for point in points:
        payload = point.payload or {}
        locations = payload.get("locations", [])
        changed = False
        for location in locations:
            if location.get("path") != relative_skill:
                continue
            for receipt in location.get("vettd_scan_publications", []):
                receipt["published_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                changed = True
        if changed:
            client.set_payload("agent_skills", {"locations": locations}, points=[point.id])


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rescan_interval_days: int | None = None):
    skill_dir = publish_scans.SEARCH_RAW / "rescan-tests" / tmp_path.parent.name / tmp_path.name / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# Demo\n", encoding="utf-8")
    relative_skill = (skill_dir / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()
    db_path = tmp_path / "qdrant"
    _indexed_db(db_path, relative_skill)
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, db_path, executable)
    if rescan_interval_days is not None:
        env["VETTD_RESCAN_INTERVAL_DAYS"] = str(rescan_interval_days)
    Path(env["HOME"]).mkdir()
    prepared = _prepare(monkeypatch, env)
    return skill_dir, db_path, env, prepared


def test_no_prior_receipt_always_rescans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir, _db_path, env, prepared = _setup(tmp_path, monkeypatch)

    summary = publish_scans.publish_skill_directories([skill_dir], prepared)

    assert (summary.succeeded, summary.skipped, summary.failed) == (1, 0, 0)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1


def test_recent_receipt_within_interval_skips_without_rehashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir, _db_path, env, prepared = _setup(tmp_path, monkeypatch)
    publish_scans.publish_skill_directories([skill_dir], prepared)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1

    # Content changes, but the last scan was seconds ago -- well inside the
    # default 7-day window -- so the time gate must skip before ever
    # hashing the folder.
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# Demo, changed\n", encoding="utf-8")
    summary = publish_scans.publish_skill_directories([skill_dir], prepared)

    assert (summary.succeeded, summary.skipped, summary.failed) == (0, 1, 0)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1


def test_past_interval_unchanged_content_skips_via_hash_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir, _db_path, env, prepared = _setup(tmp_path, monkeypatch)
    publish_scans.publish_skill_directories([skill_dir], prepared)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1

    relative_skill = (skill_dir / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()
    _backdate_receipt(prepared.client, relative_skill, days=8)

    # Folder content is unchanged; past the interval the hash check should
    # still find a match and skip -- not rescan just because time passed.
    summary = publish_scans.publish_skill_directories([skill_dir], prepared)

    assert (summary.succeeded, summary.skipped, summary.failed) == (0, 1, 0)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1


def test_past_interval_changed_content_rescans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir, _db_path, env, prepared = _setup(tmp_path, monkeypatch)
    publish_scans.publish_skill_directories([skill_dir], prepared)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1

    relative_skill = (skill_dir / "SKILL.md").relative_to(publish_scans.SEARCH_RAW).as_posix()
    _backdate_receipt(prepared.client, relative_skill, days=8)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# Demo, changed\n", encoding="utf-8")

    summary = publish_scans.publish_skill_directories([skill_dir], prepared)

    assert (summary.succeeded, summary.skipped, summary.failed) == (1, 0, 0)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 2


def test_rescan_interval_configurable_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir, _db_path, env, prepared = _setup(tmp_path, monkeypatch, rescan_interval_days=0)
    publish_scans.publish_skill_directories([skill_dir], prepared)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1

    # With a 0-day interval, an unchanged folder still skips via hash match
    # (not the time gate) on the very next run.
    summary = publish_scans.publish_skill_directories([skill_dir], prepared)
    assert (summary.succeeded, summary.skipped, summary.failed) == (0, 1, 0)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 1

    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# Demo, changed\n", encoding="utf-8")
    summary = publish_scans.publish_skill_directories([skill_dir], prepared)
    assert (summary.succeeded, summary.skipped, summary.failed) == (1, 0, 0)
    assert _scan_folder_invocations(Path(env["FAKE_LOG"])) == 2


def test_invalid_rescan_interval_env_raises_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, tmp_path / "db", executable)
    env["VETTD_RESCAN_INTERVAL_DAYS"] = "not-a-number"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(publish_scans.ConfigurationError):
        publish_scans.PublishConfig.from_env()


def test_negative_rescan_interval_env_raises_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_vettd(tmp_path)
    env = _env(tmp_path, tmp_path / "db", executable)
    env["VETTD_RESCAN_INTERVAL_DAYS"] = "-1"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(publish_scans.ConfigurationError):
        publish_scans.PublishConfig.from_env()
