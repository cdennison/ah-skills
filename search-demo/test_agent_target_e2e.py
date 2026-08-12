"""Standalone e2e test: classify_agent_target against the real, cloned
affaan-m/ECC repo (test-data/ECC), covering every signal type documented in
SKILLS_ADDITIONAL_FILES.md:

  - plugin manifests (.claude-plugin/plugin.json, .codex-plugin/plugin.json)
    claiming plain skills/tdd-workflow
  - a per-skill sidecar file (.agents/skills/tdd-workflow/agents/openai.yaml)
  - a bare path-token convention with no manifest backing it (.kiro/)
  - documentation mirrors with no signal at all (docs/{lang}/skills/...)

Run directly with `python3 test_agent_target_e2e.py` or via pytest.
Requires test-data/ECC to be cloned first (see README / prior session:
`git clone --depth 1 https://github.com/affaan-m/ECC.git test-data/ECC`).
"""

import os
import sys

from agent_target import classify_agent_target, classify_from_metadata
from aggregator_filter import is_likely_aggregator_path, find_statistical_aggregators

REPO = os.path.join(os.path.dirname(__file__), "test-data", "ECC")

# (relative path under REPO, expected agent_targets, expected confidence)
CASES = [
    (
        "skills/tdd-workflow/SKILL.md",
        ["claude-code", "codex"],
        "high",
    ),
    (
        ".agents/skills/tdd-workflow/SKILL.md",
        ["generic", "openai"],
        "high",
    ),
    (
        ".kiro/skills/tdd-workflow/SKILL.md",
        ["kiro"],
        "medium",
    ),
    (
        "docs/es/skills/tdd-workflow/SKILL.md",
        ["unknown"],
        "low",
    ),
    (
        "docs/zh-CN/skills/tdd-workflow/SKILL.md",
        ["unknown"],
        "low",
    ),
    (
        "docs/zh-TW/skills/tdd-workflow/SKILL.md",
        ["unknown"],
        "low",
    ),
    (
        "docs/ja-JP/skills/tdd-workflow/SKILL.md",
        ["unknown"],
        "low",
    ),
    (
        "docs/tr/skills/tdd-workflow/SKILL.md",
        ["unknown"],
        "low",
    ),
    (
        "docs/ko-KR/skills/tdd-workflow/SKILL.md",
        ["unknown"],
        "low",
    ),
]


def _require_repo():
    if not os.path.isdir(REPO):
        print(
            f"SKIP: {REPO} not found. Clone it first:\n"
            f"  git clone --depth 1 https://github.com/affaan-m/ECC.git {REPO}",
            file=sys.stderr,
        )
        sys.exit(1)


def test_tdd_workflow_real_files_classified_correctly():
    _require_repo()
    for rel_path, expected_targets, expected_confidence in CASES:
        full_path = os.path.join(REPO, rel_path)
        assert os.path.isfile(full_path), f"fixture missing: {full_path}"
        result = classify_agent_target(full_path)
        assert result["agent_targets"] == expected_targets, (
            f"{rel_path}: expected {expected_targets}, got {result['agent_targets']} "
            f"(evidence: {result['evidence']})"
        )
        assert result["confidence"] == expected_confidence, (
            f"{rel_path}: expected confidence {expected_confidence}, got "
            f"{result['confidence']} (evidence: {result['evidence']})"
        )
        assert result["evidence"], f"{rel_path}: evidence list must not be empty"


def test_manifest_signal_beats_path_token():
    # skills/tdd-workflow has no ".claude/" or ".codex/" path token at all --
    # its classification comes entirely from the two plugin.json manifests,
    # proving manifest resolution (not string matching on the path) is what
    # drove the "high" confidence claude-code + codex result.
    _require_repo()
    full_path = os.path.join(REPO, "skills/tdd-workflow/SKILL.md")
    result = classify_agent_target(full_path)
    assert not any(".claude/" in e or ".codex/" in e for e in result["evidence"])
    assert all("plugin manifest" in e for e in result["evidence"])


def test_sidecar_signal_beats_generic_path_token():
    # .agents/ alone would map to "generic" via the medium-tier path token,
    # but the agents/openai.yaml sidecar is a high-tier signal that adds
    # "openai" on top rather than being drowned out by the weaker token.
    _require_repo()
    full_path = os.path.join(REPO, ".agents/skills/tdd-workflow/SKILL.md")
    result = classify_agent_target(full_path)
    assert "openai" in result["agent_targets"]
    assert result["confidence"] == "high"
    assert any("sidecar file" in e for e in result["evidence"])


def test_docs_mirrors_have_no_manifest_or_sidecar_signal():
    # Confirms unknown isn't a default -- it's because nothing in
    # SKILLS_ADDITIONAL_FILES.md's "runtime" or "path token" tiers actually
    # claims these paths.
    _require_repo()
    for rel_path, expected_targets, _ in CASES:
        if not rel_path.startswith("docs/"):
            continue
        full_path = os.path.join(REPO, rel_path)
        result = classify_agent_target(full_path)
        assert result["agent_targets"] == ["unknown"]
        assert "no plugin manifest" in result["evidence"][0]


def test_every_curated_skill_in_ecc_is_classified():
    # Every real SKILL.md under skills/ (282 of them) should resolve to
    # exactly the two plugin manifests (claude-code + codex), since both
    # .claude-plugin/plugin.json and .codex-plugin/plugin.json declare
    # "./skills/" as their skills root, and nothing else claims plain
    # skills/. Two skills (lead-intelligence, continuous-learning-v2) have
    # their own agents/ subdirectory, but it holds *.md/*.sh subagent
    # definitions the skill itself invokes -- not agents/*.yaml
    # per-target-agent sidecars like .agents/skills/tdd-workflow/agents/
    # openai.yaml -- so it correctly contributes no extra signal.
    _require_repo()
    skills_root = os.path.join(REPO, "skills")
    skill_md_files = []
    for entry in sorted(os.listdir(skills_root)):
        candidate = os.path.join(skills_root, entry, "SKILL.md")
        if os.path.isfile(candidate):
            skill_md_files.append(candidate)

    assert len(skill_md_files) > 200, f"expected the full curated set, found {len(skill_md_files)}"

    for skill_md_path in skill_md_files:
        result = classify_agent_target(skill_md_path)
        assert result["confidence"] == "high", (skill_md_path, result)
        assert result["agent_targets"] == ["claude-code", "codex"], (skill_md_path, result)


def test_openclaw_path_token_from_csv_metadata():
    # No local checkout for this one -- exactly the CSV/Qdrant-scale case:
    # classify from the path string alone, as found via grep on
    # skills_export_top.csv.
    result = classify_from_metadata(
        path="Green-PT/honey-for-devs/.openclaw/skills/honey/SKILL.md",
        name="honey",
        description=(
            "Write less code and say less about it: YAGNI, stdlib-first, terse prose. "
            "Cuts agent token cost on every coding and writing task."
        ),
    )
    assert result["agent_targets"] == ["openclaw"]
    assert result["confidence"] == "medium"


def test_hermes_text_mention_from_csv_metadata():
    # This row lives inside a corpus-aggregator repo (see the aggregator
    # test below) so there's no ".hermes/" path convention to key off --
    # the only signal is the description explicitly naming "Hermes Agent".
    result = classify_from_metadata(
        path=(
            "NeverSight/learn-skills.dev/data/skills-md/amanning3390/"
            "hermeshub/agent-hardening/SKILL.md"
        ),
        name="agent-hardening",
        description=(
            "Comprehensive security hardening for Hermes Agent. Detects prompt "
            "injection, unicode smuggling, hidden directives, supply-chain skill "
            "poisoning, credential exposure, and memory manipulation."
        ),
        owner="NeverSight",
        repo="learn-skills.dev",
    )
    assert result["agent_targets"] == ["hermes"]
    assert result["confidence"] == "low"


def test_hermes_example_row_is_also_flagged_as_aggregator():
    # The same row that resolves to "hermes" via text mention independently
    # gets flagged by the structural aggregator check: "skills-md" dump dir
    # followed by a *second* owner/repo pair (amanning3390/hermeshub) before
    # reaching the skill itself.
    path = (
        "NeverSight/learn-skills.dev/data/skills-md/amanning3390/"
        "hermeshub/agent-hardening/SKILL.md"
    )
    flagged, evidence = is_likely_aggregator_path(path, owner="NeverSight", repo="learn-skills.dev")
    assert flagged
    assert "skills-md" in evidence
    assert "amanning3390/hermeshub" in evidence


def test_classify_agent_target_reads_frontmatter_when_text_not_passed():
    # Regression test: classify_agent_target(path) with no name/description
    # must read the file's own frontmatter for the low-tier text-mention
    # check, not silently skip it. Caught via ComposioHQ/awesome-claude-
    # skills' developer-growth-analysis skill, whose description literally
    # says "Claude Code chat history" -- calling classify_agent_target(path)
    # without also passing description used to return "unknown" here even
    # though classify_from_metadata() (which reads the CSV's description
    # column) correctly returned "claude-code" for the identical text.
    composio_repo = os.path.join(
        os.path.dirname(__file__), "test-data", "ComposioHQ__awesome-claude-skills"
    )
    skill_path = os.path.join(composio_repo, "developer-growth-analysis", "SKILL.md")
    if not os.path.isfile(skill_path):
        print(f"SKIP: {skill_path} not found (clone ComposioHQ/awesome-claude-skills first)", file=sys.stderr)
        return
    result = classify_agent_target(skill_path)
    assert result["agent_targets"] == ["claude-code"], result
    assert result["confidence"] == "low"
    assert any("claude code" in e for e in result["evidence"])


def test_statistical_aggregator_detection():
    # A repo where 9/10 rows content-match a *different* GitHub owner's
    # repo elsewhere in the corpus (real aggregation) should be flagged; a
    # repo with the same row count but only light cross-owner matching
    # should not.
    rows = [
        {"owner": "junkorg", "repo": "dump", "also_in": "origauthor/origrepo:origauthor/origrepo/SKILL.md" if i < 9 else ""}
        for i in range(10)
    ] + [
        {"owner": "realorg", "repo": "source", "also_in": "otherowner/otherrepo:otherowner/otherrepo/SKILL.md" if i < 2 else ""}
        for i in range(10)
    ]
    flagged = find_statistical_aggregators(rows)
    assert ("junkorg", "dump") in flagged
    assert ("realorg", "source") not in flagged


def test_self_owner_rename_is_not_flagged_as_aggregator():
    # Regression test for the false positive found on affaan-m/ECC: a repo
    # crawled under two names by the *same* owner (e.g. after a GitHub
    # rename, ECC -> everything-claude-code) must NOT be flagged just
    # because also_in points at the renamed twin -- that's the same owner,
    # not a different source.
    rows = [
        {
            "owner": "affaan-m",
            "repo": "ECC",
            "also_in": "affaan-m/everything-claude-code:affaan-m/everything-claude-code/skills/x/SKILL.md",
        }
        for _ in range(20)
    ]
    flagged = find_statistical_aggregators(rows)
    assert ("affaan-m", "ECC") not in flagged


if __name__ == "__main__":
    _require_repo()
    test_tdd_workflow_real_files_classified_correctly()
    test_manifest_signal_beats_path_token()
    test_sidecar_signal_beats_generic_path_token()
    test_docs_mirrors_have_no_manifest_or_sidecar_signal()
    test_every_curated_skill_in_ecc_is_classified()
    test_openclaw_path_token_from_csv_metadata()
    test_hermes_text_mention_from_csv_metadata()
    test_hermes_example_row_is_also_flagged_as_aggregator()
    test_classify_agent_target_reads_frontmatter_when_text_not_passed()
    test_statistical_aggregator_detection()
    test_self_owner_rename_is_not_flagged_as_aggregator()
    print(f"OK: {len(CASES)} real-file cases + corpus-scale + aggregator cases classified as expected.")
