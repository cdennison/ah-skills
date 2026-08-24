#!/usr/bin/env python3
"""Generate a promptfoo tests file + per-skill payload files from evals/skills/.

Run this whenever skills are added/changed under evals/skills/, then
`npx promptfoo@latest eval -c promptfooconfig.yaml` to (re)populate results,
and `npx promptfoo@latest view` to review/rate them in the browser.
"""

import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import scanner  # noqa: E402  (reuses build_skill_payload)

SKILLS_DIR = HERE / "skills"
PAYLOADS_DIR = HERE / "promptfoo_payloads"
TESTS_PATH = HERE / "promptfoo_tests.yaml"


def main():
    PAYLOADS_DIR.mkdir(exist_ok=True)
    tests = []
    for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        name = skill_dir.name
        payload = scanner.build_skill_payload(skill_dir)
        if not payload:
            continue
        payload_path = PAYLOADS_DIR / f"{name}.txt"
        payload_path.write_text(payload, encoding="utf-8")
        tests.append(
            {
                "description": name,
                "vars": {
                    "skill_name": name,
                    "skill_payload": f"file://promptfoo_payloads/{name}.txt",
                },
            }
        )

    TESTS_PATH.write_text(yaml.dump(tests, sort_keys=False), encoding="utf-8")
    print(f"wrote {len(tests)} test case(s) to {TESTS_PATH}")


if __name__ == "__main__":
    main()
