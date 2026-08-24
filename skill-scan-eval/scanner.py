#!/usr/bin/env python3
"""Simple Agent Skill threat scanner.

Reads a skill package, sends it to an LLM with the threat-analysis prompt,
and prints the model's analysis.

Environment variables (matching the Cisco skill-scanner):
    SKILL_SCANNER_LLM_API_KEY   API key for the provider (e.g. sk-ant-...)
    SKILL_SCANNER_LLM_MODEL     litellm model id (e.g. anthropic/claude-haiku-4-5-20251001)
"""

import json
import os
import sys
from pathlib import Path

import litellm

HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompts" / "skill_threat_analysis_prompt.md"

# File extensions treated as skill content worth sending for analysis.
TEXT_EXTS = {".md", ".py", ".sh", ".txt", ".json", ".yaml", ".yml", ".toml"}

# Structured output schema matching the format the prompt itself describes
# (findings / overall_assessment / primary_threats).
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "skill_threat_analysis",
        "schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                            },
                            "aitech": {"type": "string"},
                            "aisubtech": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "location": {"type": "string"},
                            "evidence": {"type": "string"},
                            "remediation": {"type": "string"},
                        },
                        "required": ["severity", "aitech", "title", "description"],
                    },
                },
                "overall_assessment": {"type": "string"},
                "primary_threats": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["findings", "overall_assessment", "primary_threats"],
        },
    },
}


def build_skill_payload(skill_dir: Path) -> str:
    """Concatenate all relevant files in the skill package into one blob."""
    parts = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        rel = path.relative_to(skill_dir)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        parts.append(f"### FILE: {rel}\n```\n{content}\n```")
    return "\n\n".join(parts)


def _call_llm(skill_dir: Path, structured: bool):
    api_key = os.environ.get("SKILL_SCANNER_LLM_API_KEY")
    model = os.environ.get("SKILL_SCANNER_LLM_MODEL")
    if not api_key:
        sys.exit("error: SKILL_SCANNER_LLM_API_KEY is not set")
    if not model:
        sys.exit("error: SKILL_SCANNER_LLM_MODEL is not set")

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    skill_payload = build_skill_payload(skill_dir)
    if not skill_payload:
        sys.exit(f"error: no skill files found in {skill_dir}")

    kwargs = {}
    if structured:
        kwargs["response_format"] = RESPONSE_SCHEMA

    response = litellm.completion(
        model=model,
        api_key=api_key,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Analyze the following Agent Skill package "
                    f"(directory: {skill_dir.name}):\n\n{skill_payload}"
                ),
            },
        ],
        **kwargs,
    )
    return response.choices[0].message.content


def scan(skill_dir: Path) -> str:
    """Free-text scan (original CLI behavior)."""
    return _call_llm(skill_dir, structured=False)


def scan_structured(skill_dir: Path) -> dict:
    """Scan returning the parsed findings/overall_assessment/primary_threats dict."""
    content = _call_llm(skill_dir, structured=True)
    return json.loads(content)


def main():
    default_skill = HERE / "evals" / "skills" / "safe-skills" / "simple-math"
    skill_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else default_skill
    skill_dir = skill_dir.resolve()
    if not skill_dir.is_dir():
        sys.exit(f"error: skill directory not found: {skill_dir}")

    print(f"Scanning skill: {skill_dir}\n", file=sys.stderr)
    print(scan(skill_dir))


if __name__ == "__main__":
    main()
