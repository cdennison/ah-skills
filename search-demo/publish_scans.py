#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic", "qdrant-client", "typing-extensions"]
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (no venv, no pip install needed):
#      uv run publish_scans.py DIR [DIR ...]
# 3. Or make executable and run:
#      chmod +x publish_scans.py && ./publish_scans.py DIR [DIR ...]
# ─────────────────

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from qdrant_client import QdrantClient
from qdrant_client.conversions.common_types import PointId
from qdrant_client.http.exceptions import ApiException

SEARCH_RAW: Final = Path(__file__).resolve().parent / "search-raw"
COLLECTION: Final = "agent_skills"
HELP: Final = "usage: publish_scans.py [-h] DIR [DIR ...]\n\nPublish Vettd scans for indexed skill directories."
JsonScalar: TypeAlias = str | int | float | bool | None
JsonObject: TypeAlias = dict[str, JsonScalar | list[JsonScalar] | dict[str, JsonScalar] | list[dict[str, JsonScalar]]]


@dataclass(frozen=True, slots=True)
class PublisherError(Exception):
    reason: str
    def __str__(self) -> str:
        return self.reason


ConfigurationError = PublisherError
PreflightError = PublisherError
PublishSkillError = PublisherError


@dataclass(frozen=True, slots=True)
class PublishConfig:
    vettd_cli_bin: str
    endpoint: str
    api_key: str
    expected_account_email: str
    qdrant_db_path: Path | None
    qdrant_url: str | None
    rescan_interval_days: int

    @classmethod
    def from_env(cls) -> PublishConfig:
        endpoint = os.environ.get("VETTD_SCAN_ENDPOINT", "")
        api_key = os.environ.get("VETTD_API_KEY", "")
        expected_account = os.environ.get("VETTD_EXPECTED_ACCOUNT_EMAIL", "")
        db_value = os.environ.get("SKILLS_QDRANT_DB_PATH")
        url_value = os.environ.get("SKILLS_QDRANT_URL")
        rescan_value = os.environ.get("VETTD_RESCAN_INTERVAL_DAYS", "7")
        if db_value and url_value:
            raise ConfigurationError("set only one Qdrant mode")
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.path != "/api/scans/ingest"
                or parsed.query or parsed.fragment or "?" in endpoint or "#" in endpoint):
            raise ConfigurationError("VETTD_SCAN_ENDPOINT must be an exact ingest URL")
        if not api_key:
            raise ConfigurationError("VETTD_API_KEY is required")
        if not expected_account:
            raise ConfigurationError("VETTD_EXPECTED_ACCOUNT_EMAIL is required")
        try:
            rescan_interval_days = int(rescan_value)
        except ValueError:
            raise ConfigurationError("VETTD_RESCAN_INTERVAL_DAYS must be an integer") from None
        if rescan_interval_days < 0:
            raise ConfigurationError("VETTD_RESCAN_INTERVAL_DAYS must not be negative")
        return cls(
            os.environ.get("VETTD_CLI_BIN", "vettd"), endpoint, api_key, expected_account,
            Path(db_value) if db_value else None, url_value if url_value else None,
            rescan_interval_days,
        )


@dataclass(frozen=True, slots=True)
class PreparedPublisher:
    config: PublishConfig
    client: QdrantClient
    scanner_version: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class PublishFailure:
    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class PublishSummary:
    attempted: int
    succeeded: int
    skipped: int
    failed: int
    failures: tuple[PublishFailure, ...]


class ScanMeta(BaseModel):
    scan_id: str = Field(alias="scanId", min_length=1)
    scanner_version: str = Field(alias="scannerVersion", min_length=1)


class ScanReport(BaseModel):
    scan_meta: ScanMeta = Field(alias="scanMeta")
    # Real reports nest much deeper than JsonObject models (dependencies/
    # externalScannerResults contain arrays of objects containing arrays of
    # objects) -- this is only checked for "at least one skill entry exists"
    # and never inspected further, so don't over-model content we don't use.
    skills: list[dict[str, Any]] = Field(min_length=1)


class AuthStatus(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    configured: Literal[True]
    api_key_set: Literal[True]
    reachable: Literal[True]
    account: dict[str, str]
    endpoint: str
    # Present on real `vettd auth status --json` output (scanner identity
    # fields, unrelated to account auth) but absent from this project's
    # earlier test fixtures -- not otherwise used here, just tolerated so
    # preflight doesn't reject a real CLI response over unmodeled fields.
    scanner_uuid: str | None = None
    account_uuid: str | None = None


class PayloadEnvelope(BaseModel):
    locations: list[JsonObject]


def _run(command: Sequence[str], api_key: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as error:
        rendered = " ".join("<redacted>" if part == api_key else part for part in command)
        raise PreflightError(f"command failed to start: {rendered}: {str(error).replace(api_key, '<redacted>')}") from error


def preflight(config: PublishConfig) -> PreparedPublisher:
    version_result = _run([config.vettd_cli_bin, "--version"], config.api_key)
    version_text = (version_result.stdout + version_result.stderr).strip()
    if version_result.returncode != 0 or not version_text:
        raise PreflightError(f"vettd version failed: {version_text.replace(config.api_key, '<redacted>')}")
    scanner_version = version_text.split()[-1]
    auth = _run([config.vettd_cli_bin, "auth", "--key", config.api_key, "--endpoint", config.endpoint, "--allow-public-endpoint"], config.api_key)
    if auth.returncode != 0:
        detail = (auth.stdout + auth.stderr).replace(config.api_key, "<redacted>").strip()
        raise PreflightError(f"vettd auth failed: {detail}")
    status_result = _run([config.vettd_cli_bin, "auth", "status", "--json"], config.api_key)
    try:
        status = AuthStatus.model_validate_json(status_result.stdout)
    except ValidationError:
        detail = (status_result.stdout + status_result.stderr).replace(config.api_key, "<redacted>").strip()
        raise PreflightError(f"invalid vettd auth status: {detail}") from None
    if (status_result.returncode != 0 or status.endpoint != config.endpoint
            or status.account.get("email") != config.expected_account_email):
        raise PreflightError("vettd auth status did not verify the expected target")
    client = (QdrantClient(path=str(config.qdrant_db_path)) if config.qdrant_db_path is not None
              else QdrantClient(url=config.qdrant_url or "http://localhost:6333"))
    if not client.collection_exists(COLLECTION):
        client.close()
        raise PreflightError(f"Qdrant collection {COLLECTION!r} does not exist")
    return PreparedPublisher(config, client, scanner_version, hashlib.sha256(f"{config.endpoint}\0{config.api_key}".encode()).hexdigest())


def _folder_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(
        path for path in folder.rglob("*") if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    ):
        digest.update(file_path.relative_to(folder).as_posix().encode() + b"\0")
        digest.update(file_path.read_bytes() + b"\0")
    return digest.hexdigest()


def _find_location(client: QdrantClient, relative_skill: str) -> tuple[PointId, list[JsonObject], int] | None:
    offset: PointId | None = None
    while True:
        points, offset = client.scroll(
            COLLECTION, with_payload=["locations"], with_vectors=False, limit=256, offset=offset
        )
        for point in points:
            try:
                locations = PayloadEnvelope.model_validate(point.payload or {}).locations
            except ValidationError:
                continue
            for index, location in enumerate(locations):
                if location.get("path") == relative_skill:
                    return point.id, locations, index
        if offset is None:
            return None


def _target_receipts(location: JsonObject, prepared: PreparedPublisher) -> list[dict[str, JsonScalar]]:
    receipts = location.get("vettd_scan_publications", [])
    if not isinstance(receipts, list):
        return []
    return [
        receipt for receipt in receipts
        if isinstance(receipt, dict) and receipt.get("target_fingerprint") == prepared.target_fingerprint
    ]


def _most_recent_published_at(receipts: list[dict[str, JsonScalar]]) -> datetime | None:
    timestamps: list[datetime] = []
    for receipt in receipts:
        published_at = receipt.get("published_at")
        if not isinstance(published_at, str):
            continue
        try:
            timestamps.append(datetime.fromisoformat(published_at))
        except ValueError:
            continue
    return max(timestamps) if timestamps else None


def _matching_receipt(
    receipts: list[dict[str, JsonScalar]], prepared: PreparedPublisher, content_sha256: str
) -> bool:
    return any(
        receipt.get("content_sha256") == content_sha256
        and receipt.get("scanner_version") == prepared.scanner_version
        for receipt in receipts
    )


_SEVERITY_RANK: Final = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_SEVERITY_COUNT_KEYS: Final = ("critical", "high", "medium", "low", "info")


def _findings_summary(report: ScanReport) -> dict[str, JsonValue]:
    """High-level rollup of a scan report for Qdrant: grade/trust/severity
    counts and up to 5 non-info findings (rule_id/category/severity/label
    only). Deliberately excludes each finding's `detail` text (often embeds
    file/line snippets), permissions/dependencies/consumers, and the raw
    externalScannerResults dump -- those stay in the vettd backend, not here."""
    severity_counts: dict[str, int] = dict.fromkeys(_SEVERITY_COUNT_KEYS, 0)
    categories_flagged: set[str] = set()
    all_findings: list[dict[str, Any]] = []
    grade: JsonValue = None
    trust: JsonValue = None
    for skill in report.skills:
        if grade is None:
            grade = skill.get("overallGrade")
        if trust is None:
            trust = skill.get("trustLevel")
        for scanner_result in skill.get("externalScannerResults") or []:
            for finding in scanner_result.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                severity = finding.get("severity", "info")
                if severity in severity_counts:
                    severity_counts[severity] += 1
                category = finding.get("category")
                if severity != "info" and category:
                    categories_flagged.add(category)
                all_findings.append(finding)
    has_malicious = any(
        finding.get("severity") == "critical" or finding.get("intent") == "malicious"
        for finding in all_findings
    )
    top_findings = sorted(
        (finding for finding in all_findings if finding.get("severity", "info") != "info"),
        key=lambda finding: _SEVERITY_RANK.get(finding.get("severity", "info"), -1),
        reverse=True,
    )[:5]
    return {
        "scan_id": report.scan_meta.scan_id,
        "overall_grade": grade,
        "trust_level": trust,
        "has_malicious_findings": has_malicious,
        "finding_count": len(all_findings),
        "severity_counts": severity_counts,
        "categories_flagged": sorted(categories_flagged),
        "top_findings": [
            {
                "rule_id": finding.get("ruleId", ""),
                "category": finding.get("category", ""),
                "severity": finding.get("severity", ""),
                "label": finding.get("label", ""),
            }
            for finding in top_findings
        ],
    }


def _publish_one(skill_dir: Path, prepared: PreparedPublisher) -> Literal["succeeded", "skipped"]:
    resolved = skill_dir.resolve()
    if not resolved.is_dir():
        return "skipped"
    skill_files = tuple(path for path in resolved.iterdir() if path.is_file() and path.name.casefold() == "skill.md")
    if len(skill_files) > 1:
        raise PublishSkillError("multiple case-insensitive SKILL.md files")
    if not skill_files:
        return "skipped"
    skill_file = skill_files[0]
    try:
        relative_skill = skill_file.relative_to(SEARCH_RAW.resolve()).as_posix()
    except ValueError:
        return "skipped"
    match = _find_location(prepared.client, relative_skill)
    if match is None:
        return "skipped"
    location = match[1][match[2]]

    # Rescan algorithm (see docs/ARCHITECTURE_PUBLISHING_SCANS.md): no prior
    # receipt for this target means there's nothing to gate on, so scan
    # unconditionally without hashing. Otherwise gate on time first (cheap,
    # no I/O over the folder) and only hash the folder once past the
    # interval -- an unchanged folder that's merely aged out skips via the
    # hash match below rather than rescanning just because time passed.
    target_receipts = _target_receipts(location, prepared)
    if target_receipts:
        most_recent = _most_recent_published_at(target_receipts)
        if most_recent is not None:
            age = datetime.now(UTC) - most_recent
            if age < timedelta(days=prepared.config.rescan_interval_days):
                return "skipped"
        content_sha256 = _folder_hash(resolved)
        if _matching_receipt(target_receipts, prepared, content_sha256):
            return "skipped"
    else:
        content_sha256 = _folder_hash(resolved)
    with tempfile.TemporaryDirectory(prefix="vettd-scan-") as temporary:
        report_path = Path(temporary) / "report.json"
        scan = _run(
            [prepared.config.vettd_cli_bin, "scan", "folder", str(resolved), "--deep", "--full", "--out", str(report_path), "--json"],
            prepared.config.api_key,
        )
        if scan.returncode != 0 or not report_path.is_file():
            detail = (scan.stdout + scan.stderr).replace(prepared.config.api_key, "<redacted>").strip()
            raise PublishSkillError(f"scan failed: {detail}")
        try:
            report = ScanReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise PublishSkillError(f"invalid scan report: {error}") from error
        submit = _run(
            [prepared.config.vettd_cli_bin, "scan", "submit", str(report_path), "--json"],
            prepared.config.api_key,
        )
        output_lines = [line.strip() for line in (submit.stdout + submit.stderr).splitlines()]
        accepted = any(re.fullmatch(r"Scan accepted: .+", line) for line in output_lines)
        duplicate = "Scan already submitted (duplicate)." in output_lines
        if submit.returncode != 0 or not (accepted or duplicate):
            detail = (submit.stdout + submit.stderr).replace(prepared.config.api_key, "<redacted>").strip()
            raise PublishSkillError(f"submit was not acknowledged: {detail}")
    receipts = location.get("vettd_scan_publications", [])
    prior: list[dict[str, JsonScalar]] = (
        [receipt for receipt in receipts if isinstance(receipt, dict)]
        if isinstance(receipts, list) else []
    )
    location["vettd_scan_publications"] = prior + [
        {
            "target_fingerprint": prepared.target_fingerprint,
            "endpoint": prepared.config.endpoint,
            "content_sha256": content_sha256,
            "scanner_version": prepared.scanner_version,
            "scan_id": report.scan_meta.scan_id,
            "status": "duplicate" if duplicate else "accepted",
            "published_at": datetime.now(UTC).isoformat(),
        }
    ]
    # Latest-only, not appended: vettd_scan_publications above is an
    # append-only history with no pruning (one entry per rescan, indefinitely),
    # so a findings summary attached to every historical receipt would grow
    # this point's payload without bound. This field is overwritten on every
    # publish and only ever reflects the most recent scan.
    location["vettd_scan_findings"] = _findings_summary(report)
    _ = prepared.client.set_payload(COLLECTION, {"locations": match[1]}, points=[match[0]])
    return "succeeded"


def publish_skill_directories(skill_dirs: Sequence[Path], prepared: PreparedPublisher) -> PublishSummary:
    succeeded = 0
    skipped = 0
    failures: list[PublishFailure] = []
    for skill_dir in skill_dirs:
        try:
            outcome = _publish_one(skill_dir, prepared)
        except (PublishSkillError, OSError, ApiException) as error:
            failures.append(PublishFailure(skill_dir, str(error).replace(prepared.config.api_key, "<redacted>")))
            continue
        if outcome == "succeeded":
            succeeded += 1
        else:
            skipped += 1
    return PublishSummary(len(skill_dirs), succeeded, skipped, len(failures), tuple(failures))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments or any(value in {"-h", "--help"} for value in arguments):
        print(HELP)
        return 0 if arguments else 2
    try:
        prepared = preflight(PublishConfig.from_env())
    except (ConfigurationError, PreflightError) as error:
        print(f"preflight failed: {error}", file=sys.stderr)
        return 2
    try:
        summary = publish_skill_directories(tuple(Path(value) for value in arguments), prepared)
    finally:
        prepared.client.close()
    print(f"attempted={summary.attempted} succeeded={summary.succeeded} skipped={summary.skipped} failed={summary.failed}")
    for failure in summary.failures:
        print(f"failed {failure.path}: {failure.message}", file=sys.stderr)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
