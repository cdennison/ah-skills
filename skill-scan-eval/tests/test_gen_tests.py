"""Checks on the gen_promptfoo_tests.py output (promptfoo_tests.yaml + payloads).

Regenerates the artifact fresh (same as CI/a human would before running an
eval) rather than trusting whatever is currently checked out, then verifies:

  - every skills/**/SKILL.md produced exactly one test case
  - each test case only carries vars the prompt_*.js wrappers or the shared
    assertion actually expect (skill_name, skill_payload, and optionally
    expected_safe for skills with a checked-in _expected.json) -- guards
    against a future change piling arbitrary fields into `vars`, which is
    the same anti-pattern that caused the template-substitution bug
    documented in tests/test_prompts.js (a `var` silently not being what a
    template expects)
  - every skill with expected_safe also carries the shared
    assert_expected_verdict.js assertion (see README's "Known limitations")
  - every referenced payload file actually exists and is non-empty
  - no payload file leaks _expected.json content into the model's input

Run: python -m unittest discover -s tests
(or: python tests/test_gen_tests.py)

Needs the same environment as gen_promptfoo_tests.py itself (skill-scan-eval's
.venv -- see docs/DEPLOY_TO_AWS.md for setup), since it imports scanner.py
which imports litellm.
"""

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILLS_DIR = ROOT / "skills"
TESTS_PATH = ROOT / "promptfoo_tests.yaml"
PAYLOADS_DIR = ROOT / "promptfoo_payloads"

REQUIRED_VAR_KEYS = {"skill_name", "skill_payload"}
OPTIONAL_VAR_KEYS = {"expected_safe"}
ALLOWED_VAR_KEYS = REQUIRED_VAR_KEYS | OPTIONAL_VAR_KEYS


class TestGeneratedTestCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [sys.executable, str(ROOT / "gen_promptfoo_tests.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        cls.gen_stdout = result.stdout
        cls.gen_returncode = result.returncode
        if TESTS_PATH.is_file():
            cls.tests = yaml.safe_load(TESTS_PATH.read_text(encoding="utf-8"))
        else:
            cls.tests = None

    def test_generation_succeeds(self):
        self.assertEqual(
            self.gen_returncode, 0, f"gen_promptfoo_tests.py failed:\n{self.gen_stdout}"
        )

    def test_one_test_case_per_skill(self):
        skill_dirs = sorted(p.parent for p in SKILLS_DIR.rglob("SKILL.md"))
        self.assertTrue(skill_dirs, f"no SKILL.md found under {SKILLS_DIR}")
        self.assertEqual(
            len(self.tests),
            len(skill_dirs),
            "number of generated test cases doesn't match number of SKILL.md files",
        )

    def test_each_test_case_has_only_expected_vars(self):
        for test in self.tests:
            var_keys = set(test["vars"].keys())
            self.assertTrue(
                REQUIRED_VAR_KEYS <= var_keys,
                f"test {test.get('description')!r} has vars {var_keys}, "
                f"missing required {REQUIRED_VAR_KEYS}",
            )
            self.assertTrue(
                var_keys <= ALLOWED_VAR_KEYS,
                f"test {test.get('description')!r} has vars {var_keys}, "
                f"unexpected extras beyond {ALLOWED_VAR_KEYS} -- prompt text "
                f"belongs in the prompt_*.js wrapper, not in per-test vars",
            )

    def test_expected_safe_skills_carry_the_shared_assertion(self):
        for test in self.tests:
            if "expected_safe" not in test["vars"]:
                self.assertNotIn(
                    "assert",
                    test,
                    f"test {test.get('description')!r} has an assert block but no "
                    f"expected_safe var -- assert_expected_verdict.js needs it",
                )
                continue
            self.assertIsInstance(
                test["vars"]["expected_safe"],
                bool,
                f"test {test.get('description')!r} expected_safe must be a bool",
            )
            asserts = test.get("assert", [])
            self.assertTrue(
                any(
                    a.get("type") == "javascript"
                    and a.get("value") == "file://assert_expected_verdict.js"
                    for a in asserts
                ),
                f"test {test.get('description')!r} has expected_safe but is missing "
                f"the assert_expected_verdict.js assertion",
            )

    def test_payloads_never_contain_expected_json_content(self):
        for payload_path in PAYLOADS_DIR.glob("*.txt"):
            content = payload_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "FILE: _expected.json",
                content,
                f"{payload_path} leaks _expected.json (ground truth) into the "
                f"model's input -- scanner.build_skill_payload must skip "
                f"leading-underscore files",
            )

    def test_every_payload_file_exists_and_is_non_empty(self):
        for test in self.tests:
            payload_ref = test["vars"]["skill_payload"]
            self.assertTrue(
                payload_ref.startswith("file://"),
                f"expected file:// payload reference, got {payload_ref!r}",
            )
            payload_path = ROOT / payload_ref[len("file://") :]
            self.assertTrue(payload_path.is_file(), f"missing payload file: {payload_path}")
            self.assertGreater(
                payload_path.stat().st_size,
                0,
                f"payload file is empty: {payload_path}",
            )


if __name__ == "__main__":
    unittest.main()
