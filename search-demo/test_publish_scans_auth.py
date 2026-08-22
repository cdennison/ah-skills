import subprocess
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

import publish_scans
from test_publish_scans import _env, _fake_vettd, _indexed_db, _prepare


def test_run_redacts_secret_from_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "never-expose-this-key"

    def fail_start(*_args: str | list[str], **_kwargs: bool) -> None:
        raise OSError(f"could not execute {secret}")

    monkeypatch.setattr(subprocess, "run", fail_start)
    with pytest.raises(publish_scans.PreflightError) as raised:
        publish_scans._run(["vettd", "auth", "--key", secret], secret)
    assert secret not in str(raised.value)


def test_programmer_error_propagates_from_publish_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = publish_scans.PublishConfig(
        "vettd", "https://example.test/api/scans/ingest", "key", "publisher@example.test", tmp_path, None, 7
    )
    prepared = publish_scans.PreparedPublisher(config, QdrantClient(":memory:"), "1.0", "target")

    def defect(_path: Path, _prepared: publish_scans.PreparedPublisher) -> str:
        missing: dict[str, str] = {}
        return missing["programmer-defect"]

    monkeypatch.setattr(publish_scans, "_publish_one", defect)
    try:
        with pytest.raises(KeyError, match="programmer-defect"):
            publish_scans.publish_skill_directories([tmp_path], prepared)
    finally:
        prepared.client.close()


def test_config_rejects_two_explicit_qdrant_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VETTD_API_KEY", "secret")
    monkeypatch.setenv("VETTD_SCAN_ENDPOINT", "https://example.test/api/scans/ingest")
    monkeypatch.setenv("VETTD_EXPECTED_ACCOUNT_EMAIL", "publisher@example.test")
    monkeypatch.setenv("SKILLS_QDRANT_DB_PATH", "/tmp/local")
    monkeypatch.setenv("SKILLS_QDRANT_URL", "http://qdrant:6333")

    with pytest.raises(publish_scans.ConfigurationError):
        publish_scans.PublishConfig.from_env()


@pytest.mark.parametrize(
    "endpoint",
    ["https://example.test", "https://example.test/api/scans/ingest/", "not-a-url"],
)
def test_config_rejects_non_ingest_endpoint(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("VETTD_API_KEY", "secret")
    monkeypatch.setenv("VETTD_EXPECTED_ACCOUNT_EMAIL", "publisher@example.test")
    monkeypatch.setenv("VETTD_SCAN_ENDPOINT", endpoint)

    with pytest.raises(publish_scans.ConfigurationError):
        publish_scans.PublishConfig.from_env()


def test_config_requires_expected_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VETTD_API_KEY", "secret")
    monkeypatch.setenv("VETTD_SCAN_ENDPOINT", "https://example.test/api/scans/ingest")
    monkeypatch.delenv("VETTD_EXPECTED_ACCOUNT_EMAIL", raising=False)

    with pytest.raises(publish_scans.ConfigurationError):
        publish_scans.PublishConfig.from_env()


@pytest.mark.parametrize(
    "mode", ["malformed", "false", "unconfigured", "key-unset", "missing-account", "wrong-account", "wrong-endpoint", "exit"]
)
def test_preflight_rejects_unverified_auth_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    executable = _fake_vettd(tmp_path)
    db_path = tmp_path / "qdrant"
    _indexed_db(db_path, "indexed/SKILL.md")
    overrides = {"FAKE_AUTH_STATUS_EXIT": "7"} if mode == "exit" else {"FAKE_AUTH_STATUS": mode}
    env = _env(tmp_path, db_path, executable) | overrides
    Path(env["HOME"]).mkdir()

    with pytest.raises(publish_scans.PreflightError) as raised:
        _prepare(monkeypatch, env)

    assert env["VETTD_API_KEY"] not in str(raised.value)
