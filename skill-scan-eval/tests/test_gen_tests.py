"""Checks on the gen_promptfoo_tests.py output (promptfoo_tests.yaml + payloads).

Regenerates the artifact fresh (same as CI/a human would before running an
eval) rather than trusting whatever is currently checked out, then verifies:

  - every skills/**/SKILL.md produced exactly one test case
  - each test case only carries the two vars the prompt_*.js wrappers expect
    (skill_name, skill_payload) -- guards against a future change piling
    more fields into `vars`, which is the same anti-pattern that caused the
    template-substitution bug documented in tests/test_prompts.js (a `var`
    silently not being what a template expects)
  - every referenced payload file actually exists and is non-empty

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

EXPECTED_VAR_KEYS = {"skill_name", "skill_payload"}


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

    def test_each_test_case_has_exactly_the_expected_vars(self):
        for test in self.tests:
            var_keys = set(test["vars"].keys())
            self.assertEqual(
                var_keys,
                EXPECTED_VAR_KEYS,
                f"test {test.get('description')!r} has vars {var_keys}, "
                f"expected exactly {EXPECTED_VAR_KEYS} -- prompt text belongs "
                f"in the prompt_*.js wrapper, not in per-test vars",
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
