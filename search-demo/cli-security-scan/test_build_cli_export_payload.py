"""End-to-end (hermetic) test of the Qdrant payload-write path:
report CSVs + mentions log -> `cli_security` on the point, rescan gate,
and the clear-on-no-longer-matching branch. No network.
"""

import pytest
from qdrant_client import QdrantClient, models

import _common
import build_cli_export

COLLECTION = "agent_skills"
POINT = "00000000-0000-0000-0000-0000000000a1"
SKILL_DIR = "own/repo/skills/mcp-setup"
SKILL_MD = f"{SKILL_DIR}/SKILL.md"


@pytest.fixture
def work(tmp_path, monkeypatch):
    w = tmp_path / "work"
    (w / "cache" / "osv" / "npm").mkdir(parents=True)
    monkeypatch.setattr(build_cli_export, "WORK", w)
    # osv_snapshot_date() reads _common.CACHE; point it at our tmp tree
    monkeypatch.setattr(_common, "CACHE", w / "cache")
    (w / "cache" / "osv" / "npm" / "x.json").write_text(
        '{"fetched": "2026-08-30", "result": {}}'
    )

    (w / "npm_security_report_with_skills.csv").write_text(
        "package,mentions,classification,has_bin,vuln_count,max_severity,advisory_ids,summary,skills_mentioning\n"
        f'@upstash/context7-mcp,1,cli,True,0,,,,{SKILL_DIR}\n'
        f'wrangler,1,cli,True,4,CRITICAL,"GHSA-a,GHSA-b",oops,{SKILL_DIR}\n'
    )
    (w / "pip_security_report_with_skills.csv").write_text(
        "package,mentions,classification,has_console_classifier,vuln_count,max_severity,advisory_ids,summary,skills_mentioning\n"
    )
    (w / "install_mentions.log").write_text(
        "header\n\n"
        f"{SKILL_MD}:1: claude mcp add context7 -- npx -y @upstash/context7-mcp\n"
        f"{SKILL_MD}:2: npm install -g wrangler\n"
    )
    return w


@pytest.fixture
def client(monkeypatch):
    c = QdrantClient(":memory:")
    c.create_collection(
        COLLECTION,
        vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
    )
    c.upsert(COLLECTION, points=[
        models.PointStruct(id=POINT, vector={"dense": [0.0, 0.0]},
                           payload={"path": SKILL_MD, "locations": [{"path": SKILL_MD}]}),
        models.PointStruct(id="00000000-0000-0000-0000-0000000000b2", vector={"dense": [0.0, 0.0]},
                           payload={"path": "own/repo/skills/other/SKILL.md", "locations": []}),
    ])
    monkeypatch.setattr(build_cli_export, "get_client", lambda: c, raising=False)
    import index_qdrant
    monkeypatch.setattr(index_qdrant, "get_client", lambda: c)
    return c


def _cli_security(client):
    return (client.retrieve(COLLECTION, ids=[POINT], with_payload=True)[0].payload or {}).get("cli_security")


def test_writes_verdict_with_worst_grade(work, client):
    rc = build_cli_export.run_payload(force=False, dry_run=False)
    assert rc == 0

    verdict = _cli_security(client)
    assert verdict["grade"] == "C"  # wrangler CRITICAL beats context7 clean
    pkgs = {p["package"]: p for p in verdict["packages"]}
    assert set(pkgs) == {"@upstash/context7-mcp", "wrangler"}
    assert pkgs["@upstash/context7-mcp"]["install_command"] == "claude mcp add context7 -- npx -y @upstash/context7-mcp"
    assert pkgs["wrangler"]["advisory_ids"] == ["GHSA-a", "GHSA-b"]
    # the unrelated point stays untouched
    other = client.retrieve(COLLECTION, ids=["00000000-0000-0000-0000-0000000000b2"], with_payload=True)[0]
    assert "cli_security" not in (other.payload or {})


def test_rescan_gate_skips_same_day_same_packages(work, client, capsys):
    build_cli_export.run_payload(force=False, dry_run=False)
    first = _cli_security(client)

    build_cli_export.run_payload(force=False, dry_run=False)
    assert "skipped 1" in capsys.readouterr().out
    assert _cli_security(client)["scanned_at"] == first["scanned_at"]  # untouched

    build_cli_export.run_payload(force=True, dry_run=False)
    assert _cli_security(client)["scanned_at"] != first["scanned_at"]  # rewritten


def test_clears_verdict_when_skill_no_longer_matches(work, client):
    build_cli_export.run_payload(force=False, dry_run=False)
    assert _cli_security(client) is not None

    # skill dropped all its CLI installs -> reports no longer map it
    (work / "npm_security_report_with_skills.csv").write_text(
        "package,mentions,classification,has_bin,vuln_count,max_severity,advisory_ids,summary,skills_mentioning\n"
    )
    build_cli_export.run_payload(force=False, dry_run=False)
    assert _cli_security(client) is None


def test_dry_run_writes_nothing(work, client):
    build_cli_export.run_payload(force=False, dry_run=True)
    assert _cli_security(client) is None
