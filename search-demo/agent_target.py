"""Rule-based classifier for which coding agent(s) a SKILL.md targets.

No LLM in the loop. Signals are tiered by how much they're actually trusted
to be true (see SKILLS_ADDITIONAL_FILES.md for the full writeup):

  high   - a mechanism the agent's own loader reads: a plugin manifest
           (.<agent>-plugin/plugin.json) that claims this skill's directory,
           or a per-skill sidecar file (<skill-dir>/agents/<agent>.yaml).
  medium - a structural path convention (.cursor/, .kiro/, .windsurf/, ...)
           with no manifest to back it up.
  low    - the skill's own name/description mentions an agent by name in
           prose. Weakest signal; easy to false-positive on.
  none   - nothing found -> "unknown".

Only the highest tier that produced any hits is used for the final answer;
lower tiers are recorded in evidence but don't get merged in, so a
manifest-backed "claude-code" doesn't get diluted by an unrelated name-drop
elsewhere in the description.
"""

from __future__ import annotations

import json
import os

from frontmatter import parse_frontmatter

# ---------------------------------------------------------------------------
# Tier 2 (medium): bare path tokens with no backing manifest.
# ---------------------------------------------------------------------------
AGENT_PATH_TOKENS: dict[str, str] = {
    ".claude/": "claude-code",
    ".cline/": "cline",
    ".codex/": "codex",
    ".cursor/": "cursor",
    ".kiro/": "kiro",
    ".opencode/": "opencode",
    ".windsurf/": "windsurf",
    # OpenClaw skills live under a repo-root .openclaw/skills/ dir, same
    # convention as .kiro/ -- e.g. Green-PT/honey-for-devs/.openclaw/skills/.
    ".openclaw/": "openclaw",
    # ".agents/" is a portable/shared-skills convention, not one vendor's
    # runtime dir -> "generic", not a specific agent.
    ".agents/": "generic",
}

# ---------------------------------------------------------------------------
# Tier 3 (low): explicit agent-name mentions in the skill's own text.
# ---------------------------------------------------------------------------
AGENT_NAME_VOCAB: dict[str, list[str]] = {
    "claude-code": ["claude code"],
    "cursor": ["cursor"],
    "codex": ["codex"],
    "copilot": ["copilot"],
    "gemini-cli": ["gemini cli"],
    "kiro": ["kiro"],
    "windsurf": ["windsurf"],
    "cline": ["cline"],
    "opencode": ["opencode"],
    "qwen": ["qwen"],
    "kimi": ["kimi"],
    "iflow": ["iflow"],
    "factory-droid": ["factory droid"],
    "kilocode": ["kilocode"],
    "openclaw": ["openclaw"],
    # "hermes" alone is too generic (Greek god, shipping brand, unrelated
    # libraries named Hermes) -- only match phrases that specifically name
    # the Hermes Agent project/ecosystem.
    "hermes": ["hermes agent", "hermes-agent", "hermeshub", "oh-my-hermes", "hermes skill"],
}
ROUTER_THRESHOLD = 3  # >=3 distinct agents mentioned -> router/multi-agent

# Plugin-manifest directory name -> canonical agent label.
PLUGIN_DIR_AGENT_OVERRIDES: dict[str, str] = {
    "claude": "claude-code",
}


def _find_repo_root(start_dir: str) -> str | None:
    d = os.path.abspath(start_dir)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _load_plugin_manifests(repo_root: str) -> list[tuple[str, str]]:
    """Return [(agent_label, absolute_skills_root_dir), ...] for every
    `.<agent>-plugin/plugin.json` at the repo root that declares a `skills`
    path."""
    results: list[tuple[str, str]] = []
    try:
        entries = os.listdir(repo_root)
    except OSError:
        return results

    for entry in entries:
        if not entry.endswith("-plugin"):
            continue
        manifest_path = os.path.join(repo_root, entry, "plugin.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        skills_field = manifest.get("skills")
        if not skills_field:
            continue
        skill_paths = [skills_field] if isinstance(skills_field, str) else skills_field

        raw_name = entry.lstrip(".")[: -len("-plugin")]
        agent_label = PLUGIN_DIR_AGENT_OVERRIDES.get(raw_name, raw_name)

        for rel in skill_paths:
            abs_dir = os.path.normpath(os.path.join(repo_root, rel))
            results.append((agent_label, abs_dir))

    return results


def _sidecar_agent_targets(skill_dir: str) -> list[str]:
    agents_dir = os.path.join(skill_dir, "agents")
    if not os.path.isdir(agents_dir):
        return []
    targets = []
    for fname in sorted(os.listdir(agents_dir)):
        stem, ext = os.path.splitext(fname)
        if ext.lower() in (".yaml", ".yml"):
            targets.append(stem)
    return targets


def _path_token_hit(rel_path: str) -> tuple[str, str] | None:
    haystack = rel_path + "/"
    for token, agent in AGENT_PATH_TOKENS.items():
        if token in haystack:
            return (token, agent)
    return None


def _text_mention_hits(text: str) -> list[tuple[str, str]]:
    text = text.lower()
    hits: list[tuple[str, str]] = []
    for agent, keywords in AGENT_NAME_VOCAB.items():
        for kw in keywords:
            if kw in text:
                hits.append((kw, agent))
                break
    return hits


def classify_from_metadata(
    path: str, name: str = "", description: str = "", owner: str = "", repo: str = ""
) -> dict:
    """Filesystem-free classification from CSV/payload fields alone.

    Use this at CSV/Qdrant scale where there's no local checkout to inspect
    for plugin manifests or agents/*.yaml sidecars -- it only has the
    medium (path token) and low (name mention) tiers available, so it's
    strictly more conservative than classify_agent_target() and will call
    more things "unknown". `path` should be the repo-relative skill path
    (e.g. "owner/repo/.openclaw/skills/honey/SKILL.md" as stored in
    skills_export.csv's `path` column).
    """
    path = path or ""
    token_hit = _path_token_hit(path)
    if token_hit:
        token, agent = token_hit
        return {
            "agent_targets": [agent],
            "confidence": "medium",
            "evidence": [f"path contains '{token}' -> {agent}"],
        }

    text = " ".join(part for part in (name, description, owner, repo) if part)
    hits = _text_mention_hits(text)
    agents = sorted({agent for _, agent in hits})
    evidence = [f"text mentions '{kw}' -> {agent}" for kw, agent in hits]

    if len(agents) >= ROUTER_THRESHOLD:
        return {
            "agent_targets": agents,
            "confidence": "low",
            "evidence": evidence + ["mentions >= 3 distinct agents -> router/multi-agent"],
        }
    if agents:
        return {"agent_targets": agents, "confidence": "low", "evidence": evidence}

    return {
        "agent_targets": ["unknown"],
        "confidence": "low",
        "evidence": ["no path token or agent-name mention found"],
    }


def classify_agent_target(skill_md_path: str, name: str = "", description: str = "") -> dict:
    """Best-guess classification of which agent(s) a skill targets.

    `skill_md_path` should point at a real SKILL.md inside a git checkout
    (used to walk up to the repo root and inspect plugin manifests / sidecar
    files on disk). `name`/`description` are optional extra text checked as
    a last-resort, low-confidence signal -- if omitted, they're read from
    the file's own frontmatter (so callers don't silently lose the text-
    mention tier just by not re-parsing the file themselves).

    Returns {"agent_targets": [...], "confidence": "high"|"medium"|"low",
    "evidence": [...]}. Falls back to {"agent_targets": ["unknown"],
    "confidence": "low", ...} when nothing is found.
    """
    skill_md_path = os.path.abspath(skill_md_path)
    skill_dir = os.path.dirname(skill_md_path)
    evidence: list[str] = []

    if not name and not description:
        try:
            with open(skill_md_path, encoding="utf-8") as f:
                fm = parse_frontmatter(f.read())
            name = fm.get("name", "")
            description = fm.get("description", "")
        except OSError:
            pass

    high_targets: set[str] = set()
    repo_root = _find_repo_root(skill_dir)

    # --- high tier: plugin manifests claiming this skill's directory ---
    if repo_root:
        for agent_label, skills_root in _load_plugin_manifests(repo_root):
            if skill_dir == skills_root or skill_dir.startswith(skills_root + os.sep):
                high_targets.add(agent_label)
                rel_manifest = os.path.relpath(skills_root, repo_root)
                evidence.append(
                    f"plugin manifest claims '{rel_manifest}' -> {agent_label}"
                )

    # --- high tier: per-skill agents/<agent>.yaml sidecar files ---
    for agent_label in _sidecar_agent_targets(skill_dir):
        high_targets.add(agent_label)
        evidence.append(f"sidecar file agents/{agent_label}.yaml -> {agent_label}")

    # Path token is computed regardless of tier: when a high-tier hit
    # already exists, a structural token like ".agents/" -> "generic" is
    # still real corroborating signal and gets folded in rather than
    # discarded just because a sidecar file also fired.
    if repo_root:
        rel_path = os.path.relpath(skill_md_path, repo_root)
    else:
        rel_path = skill_md_path
    path_token_hit: tuple[str, str] | None = None
    for token, agent in AGENT_PATH_TOKENS.items():
        if token in (rel_path + "/"):
            path_token_hit = (token, agent)
            break

    if high_targets:
        if path_token_hit:
            token, agent = path_token_hit
            high_targets.add(agent)
            evidence.append(f"path also contains '{token}' -> {agent}")
        return {
            "agent_targets": sorted(high_targets),
            "confidence": "high",
            "evidence": evidence,
        }

    # --- medium tier: bare path token, no manifest to back it ---
    if path_token_hit:
        token, agent = path_token_hit
        return {
            "agent_targets": [agent],
            "confidence": "medium",
            "evidence": [f"path contains '{token}' with no backing plugin manifest -> {agent}"],
        }

    # --- low tier: explicit agent-name mention in the skill's own text ---
    text_hits = _text_mention_hits(f"{name} {description}")
    agents = sorted({agent for _, agent in text_hits})
    text_evidence = [f"text mentions '{kw}' -> {agent}" for kw, agent in text_hits]

    if len(agents) >= ROUTER_THRESHOLD:
        return {
            "agent_targets": agents,
            "confidence": "low",
            "evidence": text_evidence + ["mentions >= 3 distinct agents -> router/multi-agent"],
        }
    if agents:
        return {"agent_targets": agents, "confidence": "low", "evidence": text_evidence}

    # --- nothing found ---
    return {
        "agent_targets": ["unknown"],
        "confidence": "low",
        "evidence": ["no plugin manifest, sidecar file, path token, or agent-name mention found"],
    }
