"""Hermetic tests for grading + verdict assembly (no network, no Qdrant)."""

from build_cli_export import build_verdict, grade_for_package, worst_grade


def test_grade_for_package():
    assert grade_for_package(0, "NONE") == "A"
    assert grade_for_package(0, "") == "A"
    assert grade_for_package(3, "LOW") == "B"
    assert grade_for_package(1, "MODERATE") == "B"
    assert grade_for_package(1, "MEDIUM") == "B"
    assert grade_for_package(2, "HIGH") == "C"
    assert grade_for_package(1, "CRITICAL") == "C"
    # advisory present but OSV gave no recognized label -> conservative C
    assert grade_for_package(1, "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == "C"
    assert grade_for_package(1, "") == "C"
    # non-numeric vuln_count is treated as 0
    assert grade_for_package("?", "HIGH") == "A"


def test_worst_grade():
    assert worst_grade(["A", "A"]) == "A"
    assert worst_grade(["A", "B", "A"]) == "B"
    assert worst_grade(["B", "C"]) == "C"
    assert worst_grade([]) == "A"


def _pkg_info(**over):
    base = {"classification": "cli", "vuln_count": 0, "max_severity": "NONE", "advisory_ids": []}
    base.update(over)
    return base


def test_build_verdict_none_when_no_packages():
    assert build_verdict({"skill-x"}, {}, {}, {}, "2026-08-30") is None


def test_build_verdict_shape_and_worst_grade():
    skill = "owner/repo/skills/foo"
    skill_to_packages = {skill: {("npm", "playwright"), ("pip", "ruff")}}
    pkg_info = {
        ("npm", "playwright"): _pkg_info(vuln_count=2, max_severity="HIGH", advisory_ids=["GHSA-1"]),
        ("pip", "ruff"): _pkg_info(),
    }
    commands = {(skill, "npm", "playwright"): "npm install -g playwright"}

    verdict = build_verdict({skill}, skill_to_packages, pkg_info, commands, "2026-08-30")

    assert verdict["grade"] == "C"  # worst of C (playwright) and A (ruff)
    assert verdict["osv_snapshot_date"] == "2026-08-30"
    assert [p["package"] for p in verdict["packages"]] == ["playwright", "ruff"]  # sorted by (eco, pkg)
    pw = next(p for p in verdict["packages"] if p["package"] == "playwright")
    assert pw["ecosystem"] == "npm"
    assert pw["install_command"] == "npm install -g playwright"
    assert pw["advisory_ids"] == ["GHSA-1"]
    ruff = next(p for p in verdict["packages"] if p["package"] == "ruff")
    assert ruff["install_command"] == ""  # no command recorded -> empty, not fabricated


def test_build_verdict_unions_across_a_skills_locations():
    a, b = "owner/repo/skills/a", "owner/repo/skills/b"
    skill_to_packages = {a: {("npm", "x")}, b: {("pip", "y")}}
    pkg_info = {("npm", "x"): _pkg_info(), ("pip", "y"): _pkg_info(vuln_count=1, max_severity="LOW")}

    verdict = build_verdict({a, b}, skill_to_packages, pkg_info, {}, "2026-08-30")

    assert {p["package"] for p in verdict["packages"]} == {"x", "y"}
    assert verdict["grade"] == "B"
