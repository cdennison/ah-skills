"""Structural checks on promptfooconfig.yaml.

Companion to tests/test_prompts.js (which checks the prompt_*.js wrappers
actually produce correct, fully-substituted messages). This file checks the
config that wires those wrappers to providers/tests doesn't regress into the
patterns that caused past bugs:

  - a prompt file:// reference pointing at something that doesn't exist
  - a provider that isn't routed through OpenRouter (silently requiring a
    direct ANTHROPIC_API_KEY/OPENAI_API_KEY/etc again)
  - reintroducing `defaultTest.vars` to carry full prompt text (that's the
    exact shape of the bug in tests/test_prompts.js's docstring: a promptfoo
    `{{var}}` silently failing to substitute is invisible to promptfoo's own
    pass/fail reporting -- 0 errors, garbage content. Prompt text now lives
    directly in the prompt_*.js wrappers instead.)

Run: python -m unittest discover -s tests
(or: python tests/test_config.py)
"""

import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = ROOT / "promptfooconfig.yaml"


class TestPromptfooConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    def test_config_file_exists(self):
        self.assertTrue(CONFIG_PATH.is_file())

    def test_every_prompt_file_reference_resolves(self):
        prompts = self.config["prompts"]
        self.assertTrue(prompts, "prompts list must not be empty")
        for entry in prompts:
            prompt_id = entry["id"]
            self.assertTrue(
                prompt_id.startswith("file://"),
                f"expected a file:// prompt id, got {prompt_id!r}",
            )
            rel_path = prompt_id[len("file://") :]
            resolved = ROOT / rel_path
            self.assertTrue(
                resolved.is_file(),
                f"prompt {prompt_id!r} (label={entry.get('label')!r}) does not exist on disk: {resolved}",
            )

    def test_prompt_labels_are_unique_and_vendor_prefixed(self):
        labels = [entry["label"] for entry in self.config["prompts"]]
        self.assertEqual(
            len(labels), len(set(labels)), f"duplicate prompt labels: {labels}"
        )
        known_vendors = ("cisco_", "nvidia_")
        for label in labels:
            self.assertTrue(
                label.startswith(known_vendors),
                f"label {label!r} should be prefixed with its originating "
                f"project ({known_vendors}) so results are unambiguous about "
                f"provenance -- see promptfooconfig.yaml header comment",
            )

    def test_all_providers_route_through_openrouter(self):
        providers = self.config["providers"]
        self.assertTrue(providers, "providers list must not be empty")
        for entry in providers:
            provider_id = entry["id"]
            self.assertTrue(
                provider_id.startswith("openrouter:"),
                f"provider {provider_id!r} bypasses OpenRouter -- this would "
                f"silently require its own direct API key again",
            )

    def test_no_full_prompt_text_smuggled_through_default_test_vars(self):
        default_test = self.config.get("defaultTest") or {}
        vars_ = default_test.get("vars") or {}
        for name, value in vars_.items():
            self.assertFalse(
                name.startswith("system_prompt"),
                f"defaultTest.vars.{name} reintroduces the old file://-loaded "
                f"system-prompt-as-var pattern -- this is what caused a "
                f"template substitution to silently fail before. Load the "
                f"prompt text directly inside the prompt_*.js wrapper instead.",
            )

    def test_tests_file_reference_resolves(self):
        tests_id = self.config["tests"]
        self.assertTrue(tests_id.startswith("file://"))
        resolved = ROOT / tests_id[len("file://") :]
        self.assertTrue(resolved.is_file(), f"tests file missing: {resolved}")


if __name__ == "__main__":
    unittest.main()
