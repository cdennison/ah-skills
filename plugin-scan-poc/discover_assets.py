#!/usr/bin/env python3
"""discover_assets.py — deterministic asset discovery for coding-agent plugin repos.

Point it at a repo. It works out what a user's machine actually receives when
they install / load each plugin in the repo, then enumerates every file inside
those *install surfaces* that could carry a security issue — something that runs
on the user's machine, gets loaded into an LLM's context, or is a
manifest/config that wires those together — and writes a JSON catalogue.

It does NOT scan for threats. It only finds and classifies the targets a
scanner must then cover. File contents are read only for *structure* (shebang
line, JSON keys, `@`-imports, the command strings inside a hooks manifest) so
that referenced scripts are not missed.

Model
-----
An **install surface** is a directory whose contents reach a user machine as a
unit:
  * `plugin`        — a marketplace entry's `source` dir (`/plugin install …`)
  * `plugin_dir`    — a dir with `.claude-plugin/plugin.json` (`--plugin-dir`)
  * `standalone`    — a `.claude/` `.cursor/` `.codex/` … dir loaded when the
                      repo is opened in that harness
  * `skill_source`  — a source skill dir transformed into the per-harness copies

Files under a surface are **assets** (classified, scan-typed). Files under no
surface are **orphans** (reported, not scanned — but executables among them are
listed, and separately-distributed components like a browser extension or an
npm CLI are named).

Scope: coding-agent plugins. `hermes` and `openclaw` surfaces are recorded
under `excluded`, never scanned.

Usage:
    python discover_assets.py /path/to/repo [-o out.json] [--stdout]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCANNER_VERSION = "0.4.0"

# ─────────────────────────────────────────────────────────────────────────────
# Static tables
# ─────────────────────────────────────────────────────────────────────────────

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".gradle",
    "dist", "build", ".next", ".turbo", ".parcel-cache", ".cache",
    "target", "vendor", "Pods", ".idea", "coverage", "site-packages",
}

# Surfaces are never rooted inside these (they contain deliberate copies).
SURFACE_FORBIDDEN_SEGMENTS = {"tests", "test", "fixtures", "__fixtures__",
                              "demos", "examples", "node_modules", ".git"}

EXCLUDED_HARNESSES = {"hermes", "openclaw", "claw"}
EXCLUDED_HARNESS_DIRS = {
    ".hermes", ".hermes-plugin", "hermes-plugin",
    ".openclaw", ".openclaw-plugin", "openclaw-plugin", ".claw",
}

# Directory names that are a per-harness standalone config surface.
HARNESS_DOT_DIRS = {
    ".claude": "claude-code", ".cursor": "cursor", ".codex": "codex",
    ".gemini": "gemini", ".grok": "grok", ".opencode": "opencode",
    ".pi": "pi", ".agent": "agent-std", ".agents": "agent-std",
    ".kiro": "kiro", ".qoder": "qoder", ".rovodev": "rovodev",
    ".trae": "trae", ".trae-cn": "trae-cn", ".veto": "veto",
    ".vibe": "vibe", ".windsurf": "windsurf", ".aider": "aider",
    ".continue": "continue", ".cline": "cline", ".roo": "roo",
    ".github": "github",
}
# markers that make a dot-dir a real config surface
SURFACE_MARKER_NAMES = {"skills", "commands", "agents", "hooks", "rules",
                        "hooks.json", "settings.json", "mcp.json", ".mcp.json"}

INERT_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp",
    ".tif", ".tiff", ".psd", ".ai", ".sketch",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".mp3", ".wav", ".mov", ".webm", ".avi", ".flac", ".ogg",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".jar", ".war", ".class", ".pyc", ".pyo", ".so", ".dylib", ".dll",
    ".a", ".o", ".obj", ".lib", ".wasm", ".node",
}
INERT_BASENAMES = {
    ".ds_store", "thumbs.db", ".gitignore", ".gitattributes", ".gitmodules",
    ".editorconfig", ".npmignore", ".dockerignore", ".prettierignore",
    ".eslintignore", ".nvmrc", ".node-version", ".python-version",
    ".ruby-version", ".tool-versions", ".gitkeep", "py.typed",
}
INERT_BASENAME_PREFIXES = ("license", "licence", "copying", "notice", "authors")
LOCKFILE_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "cargo.lock", "poetry.lock", "uv.lock", "pdm.lock", "composer.lock",
    "gemfile.lock", "bun.lock", "bun.lockb", "flake.lock", "go.sum",
}

CODE_EXT = {
    ".sh", ".bash", ".zsh", ".fish", ".ksh", ".command",
    ".py", ".pyw", ".rb", ".pl", ".pm", ".lua", ".php",
    ".js", ".cjs", ".mjs", ".jsx", ".ts", ".tsx", ".mts", ".cts",
    ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".vbs", ".wsf",
    ".r", ".jl", ".tcl", ".awk", ".sed", ".nu", ".exp",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".m", ".mm",
    ".java", ".kt", ".kts", ".scala", ".groovy", ".clj", ".ex", ".exs",
    ".dart", ".swift", ".zig", ".v", ".nim",
}
WEB_EXT = {".html", ".htm", ".xhtml", ".svg", ".vue", ".svelte", ".astro"}
DATA_EXT = {
    ".json", ".json5", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".properties", ".xml", ".plist", ".env",
}
DOC_EXT = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"}

CONTEXT_FILE_BASENAMES = {
    "claude.md", "agents.md", "agent.md", "gemini.md", "qwen.md", "cursor.md",
    "windsurf.md", "codex.md", ".cursorrules", ".windsurfrules",
    ".clinerules", ".goosehints", "conventions.md", "llms.txt", "llms-full.txt",
}
COMPONENT_DIRS = {"skills", "commands", "agents", "hooks", "monitors",
                  "workflows", "output-styles", "outputstyles", "themes", "bin"}

MANIFEST_BASENAMES = {
    "plugin.json", "gemini-extension.json", "opencode.json", "opencode.jsonc",
}

RELEVANCE_ORDER = {"high": 3, "medium": 2, "low": 1}

PATH_VARS = ("${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_DATA}",
             "${CLAUDE_PROJECT_DIR}", "${CURSOR_PLUGIN_ROOT}",
             "$CLAUDE_PLUGIN_ROOT", "$CLAUDE_PROJECT_DIR", "${PWD}")


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

class Surface:
    __slots__ = ("id", "kind", "root", "harness", "install", "copies", "scoped_roots")

    def __init__(self, id, kind, root, harness, install, copies, scoped_roots=None):
        self.id = id
        self.kind = kind
        self.root = root            # "" == repo root
        self.harness = harness
        self.install = install
        self.copies = copies
        # when set, the surface only covers these sub-paths (+ the manifest dir),
        # not the whole subtree under `root`
        self.scoped_roots = scoped_roots

    def _under(self, relpath: str, base: str) -> bool:
        if base == "":
            return True
        return relpath == base or relpath.startswith(base + "/")

    def contains(self, relpath: str) -> bool:
        if self.scoped_roots is not None:
            return any(self._under(relpath, b) for b in self.scoped_roots)
        return self._under(relpath, self.root)

    def to_dict(self, file_count, asset_count):
        return {
            "id": self.id, "kind": self.kind,
            "root": self.root or ".", "harness": self.harness,
            "install": self.install, "copies": self.copies,
            "files": file_count, "assets": asset_count,
        }


class Asset:
    __slots__ = ("path", "kind", "scan_type", "relevance", "description",
                 "harnesses", "surfaces", "signals", "referenced_by", "references")

    def __init__(self, path, kind, scan_type, relevance, description,
                 harnesses=None, signals=None):
        self.path = path
        self.kind = kind
        self.scan_type = scan_type
        self.relevance = relevance
        self.description = description
        self.harnesses = set(harnesses or [])
        self.surfaces = set()
        self.signals = list(signals or [])
        self.referenced_by = set()
        self.references = set()

    def bump(self, relevance):
        if RELEVANCE_ORDER[relevance] > RELEVANCE_ORDER[self.relevance]:
            self.relevance = relevance

    def to_dict(self):
        return {
            "path": self.path,
            "kind": self.kind,
            "scan_type": self.scan_type,
            "security_relevance": self.relevance,
            "harnesses": sorted(self.harnesses) or ["shared"],
            "surfaces": sorted(self.surfaces),
            "description": self.description,
            "signals": sorted(set(self.signals)),
            "referenced_by": sorted(self.referenced_by),
            "references": sorted(self.references),
        }


# ─────────────────────────────────────────────────────────────────────────────
# fs helpers
# ─────────────────────────────────────────────────────────────────────────────

def rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def segs(relpath: str) -> list[str]:
    return relpath.split("/") if relpath else []


def read_text(p: Path, limit: int = 512 * 1024) -> str | None:
    try:
        data = p.read_bytes()[:limit]
    except OSError:
        return None
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


def has_shebang(p: Path) -> str | None:
    try:
        with p.open("rb") as fh:
            first = fh.readline(200)
    except OSError:
        return None
    return first.decode("utf-8", errors="replace").strip() if first.startswith(b"#!") else None


def is_executable(p: Path) -> bool:
    try:
        return bool(p.stat().st_mode & 0o111) and p.is_file()
    except OSError:
        return False


def looks_binary(p: Path) -> bool:
    try:
        return b"\x00" in p.open("rb").read(8192)
    except OSError:
        return True


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield Path(dirpath) / fn


# ─────────────────────────────────────────────────────────────────────────────
# Surface discovery
# ─────────────────────────────────────────────────────────────────────────────

def _forbidden(relpath: str) -> bool:
    return any(s in SURFACE_FORBIDDEN_SEGMENTS for s in segs(relpath))


def _excluded_harness_path(relpath: str) -> bool:
    low = [s.lower() for s in segs(relpath)]
    return any(s in EXCLUDED_HARNESS_DIRS or s in EXCLUDED_HARNESSES for s in low)


def _manifest_component_dirs(manifest: Path, root: Path) -> list[str]:
    """Component dirs a plugin.json names (default 'skills'/'commands'/... when silent)."""
    plugin_root = manifest.parent.parent
    txt = read_text(manifest)
    dirs: set[str] = set()
    data = {}
    if txt:
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            pass
    for key in ("skills", "commands", "agents", "hooks", "mcpServers",
                "lspServers", "monitors", "outputStyles", "bin"):
        val = data.get(key)
        cands = []
        if isinstance(val, str):
            cands = [val]
        elif isinstance(val, list):
            cands = [v for v in val if isinstance(v, str)]
        elif isinstance(val, dict) or val is None:
            cands = [key]  # default location
        for c in cands:
            while c.startswith("./"):
                c = c[2:]
            c = c.rstrip("/")
            for anchor in (plugin_root, root):
                cand = anchor / c
                if cand.exists():
                    try:
                        dirs.add(rel(cand if cand.is_dir() else cand.parent, root))
                    except ValueError:
                        pass
    # always include the conventional dirs that exist
    for d in ("skills", "commands", "agents", "hooks", "mcpServers", ".mcp.json"):
        if (plugin_root / d).exists():
            dirs.add(rel(plugin_root / d, root))
    return sorted(d for d in dirs if d and d != ".")


def find_surfaces(root: Path):
    surfaces: dict[str, Surface] = {}
    external: list[dict] = []
    notes: list[str] = []

    def add(s: Surface):
        surfaces.setdefault(s.id, s)

    # 1. marketplace.json entries
    for mp in root.rglob(".claude-plugin/marketplace.json"):
        rp = rel(mp, root)
        if _forbidden(rp) or _excluded_harness_path(rp):
            continue
        mkt_root = mp.parent.parent  # dir containing .claude-plugin/
        txt = read_text(mp)
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            notes.append(f"{rp}: unparseable marketplace.json")
            continue
        mkt_name = data.get("name", "marketplace")
        for entry in data.get("plugins", []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "?")
            src = entry.get("source", ".")
            if isinstance(src, dict):
                external.append({"marketplace": mkt_name, "entry": name,
                                 "source": src})
                notes.append(f"{rp}: entry '{name}' installs from an external "
                             f"source ({src.get('source')}); not resolvable locally")
                continue
            srcdir = (mkt_root / src).resolve()
            try:
                sroot = rel(srcdir, root)
            except ValueError:
                notes.append(f"{rp}: entry '{name}' source '{src}' escapes the repo")
                continue
            sroot = "" if sroot == "." else sroot
            if not (root / sroot).is_dir():
                notes.append(f"{rp}: entry '{name}' source '{src}' is not a directory")
                continue
            add(Surface(f"marketplace:{mkt_name}/{name}", "plugin", sroot,
                        "claude-code",
                        f"/plugin marketplace add <repo> ; /plugin install {name}@{mkt_name}",
                        "whole subtree copied to ~/.claude/plugins/cache/"
                        f"{mkt_name}/{name}/<version>/ (+ npm install if package.json)"))

    # 2. every .claude-plugin/plugin.json  (plugin_dir)
    have_subdir_surface = any(
        s.root not in ("", ".") for s in surfaces.values())
    for pj in root.rglob(".claude-plugin/plugin.json"):
        rp = rel(pj, root)
        if _forbidden(rp) or _excluded_harness_path(rp):
            continue
        proot = rel(pj.parent.parent, root)
        proot = "" if proot == "." else proot
        if any(s.root == proot and s.kind == "plugin" for s in surfaces.values()):
            continue  # already a marketplace surface
        scoped = None
        note = "whole subtree"
        if proot == "" and have_subdir_surface:
            # a whole-repo manifest that coexists with a subdir plugin: scope it
            # to the manifest dir + the component dirs it actually names, so the
            # rest of the monorepo is not swept in.
            comp = _manifest_component_dirs(pj, root)
            scoped = [".claude-plugin"] + comp
            note = ("manifest root + component dirs it names (" + ", ".join(comp or ["defaults"])
                    + "); the repo also ships a narrower plugin dir")
        add(Surface(f"plugin_dir:{proot or '.'}", "plugin_dir", proot, "claude-code",
                    f"claude --plugin-dir {proot or '.'}  (or a marketplace pointing here)",
                    note, scoped_roots=scoped))

    # 3. other-harness plugin manifests  .<h>-plugin/plugin.json
    for pj in root.rglob("plugin.json"):
        rp = rel(pj, root)
        if _forbidden(rp) or _excluded_harness_path(rp):
            continue
        parent = pj.parent.name.lower()
        m = re.match(r"\.([a-z0-9]+)-plugin$", parent)
        if not m:
            continue
        h = m.group(1)
        if h in EXCLUDED_HARNESSES or h == "claude":  # .claude-plugin is rule 2's job
            continue
        proot = rel(pj.parent.parent, root)
        proot = "" if proot == "." else proot
        add(Surface(f"plugin_dir:{h}:{proot or '.'}", "plugin_dir", proot, h,
                    f"{h} plugin install pointing at this dir", "whole subtree"))

    # 4. standalone dot-dir config surfaces (repo-root children only)
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name not in HARNESS_DOT_DIRS:
            continue
        if child.name.lower() in EXCLUDED_HARNESS_DIRS:
            continue
        names = {c.name for c in child.iterdir()} if child.is_dir() else set()
        if child.name == ".github":
            markers = names & {"skills", "hooks", "agents"}
            if not markers:
                continue
        elif not (names & SURFACE_MARKER_NAMES):
            continue
        h = HARNESS_DOT_DIRS[child.name]
        add(Surface(f"standalone:{child.name}", "standalone", child.name, h,
                    f"present in the repo checkout; {h} loads it when the repo is opened",
                    "the config dir's own subtree"))

    # 5. skill-source dirs (repo-root children holding SKILL.md / SKILL.src.md)
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        if child.name in SURFACE_FORBIDDEN_SEGMENTS or child.name in COMPONENT_DIRS:
            continue
        if any(s.root == child.name for s in surfaces.values()):
            continue
        hit = any(f.name.lower().startswith("skill")
                  and f.suffix.lower() in DOC_EXT
                  for f in child.rglob("*") if f.is_file()
                  and "reference" not in f.parts and len(f.relative_to(child).parts) <= 3)
        if hit:
            add(Surface(f"skill_source:{child.name}", "skill_source", child.name,
                        "(source template)",
                        "not installed directly; a build step transforms it into "
                        "the per-harness copies",
                        "the source skill subtree"))

    return list(surfaces.values()), external, notes


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

def classify(p: Path, root: Path, harnesses: set):
    relpath = rel(p, root)
    S = segs(relpath)
    Sl = [s.lower() for s in S]
    base = p.name
    low = base.lower()
    ext = p.suffix.lower()

    if _excluded_harness_path(relpath):
        return None, {"path": relpath, "reason": "out-of-scope harness (hermes/openclaw)"}

    # hard-inert
    if low in INERT_BASENAMES or any(low.startswith(x) for x in INERT_BASENAME_PREFIXES):
        return None, {"path": relpath, "reason": "inert metadata / license file"}
    if low in LOCKFILE_BASENAMES:
        return None, {"path": relpath, "reason": "dependency lockfile (CLI/dependency scan, not asset discovery)"}
    if ext in INERT_EXT:
        return None, {"path": relpath, "reason": f"inert binary/media ({ext})"}

    if Sl[:2] == [".github", "workflows"] and ext in {".yml", ".yaml"}:
        return Asset(relpath, "ci_workflow", "config", "low",
                     "CI workflow — runs in the author's CI, not on the user's machine, "
                     "but produces the artifact/commit users install. Review for "
                     "release-pipeline tampering.", set(), ["ci"]), None
    if Sl[:1] == [".github"] and not any(x in Sl for x in ("skills", "hooks", "agents")):
        return None, {"path": relpath, "reason": "GitHub repo metadata"}

    shebang = has_shebang(p)
    execbit = is_executable(p)
    in_component = any(s in COMPONENT_DIRS for s in Sl)
    is_exampleish = any(k in relpath.lower() for k in
                        ("example", "sample", "fixture", "/mocks/", "/__mocks__/", "/golden/"))

    # manifests
    if low in MANIFEST_BASENAMES or low.endswith("plugin.json"):
        return Asset(relpath, "manifest:plugin", "manifest", "medium",
                     "Plugin/extension manifest — identity + component path overrides. "
                     "Parsed for hooks / mcpServers / commands / agents / skills / bin.",
                     harnesses, ["manifest"]), None
    if low == "marketplace.json":
        return Asset(relpath, "manifest:marketplace", "manifest", "medium",
                     "Marketplace manifest — plugin entries + their `source`. Parsed for "
                     "source type and per-entry overrides.", harnesses, ["manifest"]), None
    if low == "package.json":
        return Asset(relpath, "manifest:npm", "manifest", "medium",
                     "Node package manifest. Parsed for lifecycle `scripts` "
                     "(preinstall/install/postinstall/prepare — run on `npm install`), "
                     "`bin`, `main`.", harnesses, ["manifest", "npm"]), None
    if low in {"pyproject.toml", "setup.py", "setup.cfg"}:
        return Asset(relpath, "manifest:python", "manifest", "medium",
                     "Python package manifest — build hooks / entry points may run on install.",
                     harnesses, ["manifest"]), None

    # skills / commands / agents
    if low == "skill.md" or (low.startswith("skill") and low.endswith((".md", ".src.md"))
                             and "reference" not in Sl):
        name = S[-2] if len(S) >= 2 and S[-2].lower() != "skills" else (S[-3] if len(S) >= 3 else "(root)")
        return Asset(relpath, "skill", "skill", "medium",
                     f"Agent Skill '{name}' entrypoint — instructions loaded into the model. "
                     f"Indirect-prompt-injection / policy surface; reuse the skill scanner.",
                     harnesses, ["skill-entrypoint"]), None
    if len(S) >= 2 and S[-2].lower() == "commands" and ext in DOC_EXT:
        return Asset(relpath, "command", "skill", "medium",
                     "Slash-command file — instructions loaded into the model.",
                     harnesses, ["skill-entrypoint"]), None
    if len(S) >= 2 and S[-2].lower() == "agents" and ext in DOC_EXT | {".toml", ".yaml", ".yml"}:
        return Asset(relpath, "agent", "skill", "medium",
                     "Subagent definition — system prompt + tool grants for a dispatched "
                     "agent (a plugin can make one the main thread). Injection / "
                     "over-broad-tool surface.", harnesses, ["agent-definition"]), None

    # hook / mcp / lsp / monitor configs
    if low == "hooks.json" or (len(S) >= 2 and S[-2].lower() == "hooks" and ext == ".json") \
       or re.match(r"hooks-[a-z0-9]+\.json$", low):
        return Asset(relpath, "hook_config", "wiring", "high",
                     "Hook manifest — binds harness events (SessionStart, PreToolUse, "
                     "PostToolUse, Stop, …) to commands / http / prompts / agents. "
                     "Parsed to resolve every script it points at.", harnesses,
                     ["hook-manifest"]), None
    if low in {".mcp.json", "mcp.json"}:
        return Asset(relpath, "mcp_config", "wiring", "high",
                     "MCP server manifest — command / args / env for servers that start "
                     "when the plugin is enabled. Parsed to resolve local server binaries.",
                     harnesses, ["mcp-manifest"]), None
    if low in {".lsp.json", "lsp.json"}:
        return Asset(relpath, "lsp_config", "wiring", "medium",
                     "LSP server manifest — launches a language-server binary from PATH.",
                     harnesses, ["lsp-manifest"]), None
    if low == "monitors.json" or (len(S) >= 2 and S[-2].lower() == "monitors" and ext == ".json"):
        return Asset(relpath, "monitor_config", "wiring", "high",
                     "Background-monitor manifest — long-running commands started "
                     "automatically while the plugin is active.", harnesses,
                     ["monitor-manifest"]), None
    if low == "settings.json":
        return Asset(relpath, "settings", "wiring", "high",
                     "Settings file — may carry a `hooks` block, or (plugin settings.json) "
                     "force-activate one of the plugin's agents as the main thread.",
                     harnesses, ["settings"]), None

    # non-Claude injection entrypoints (executable plugin code)
    if (".opencode" in Sl or ".pi" in Sl) and ext in CODE_EXT and (
            "plugin" in Sl or "extension" in Sl or "plugins" in Sl or "extensions" in Sl):
        h = "opencode" if ".opencode" in Sl else "pi"
        return Asset(relpath, "injection_entrypoint", "code", "high",
                     f"{h} plugin/extension code — runs in the {h} process; typically "
                     f"registers skills and injects bootstrap context automatically.",
                     {h}, ["executable-code", "auto-runs", "context-injection"]), None

    # context files
    if low in CONTEXT_FILE_BASENAMES:
        return Asset(relpath, "context_file", "wiring", "high",
                     "Context file — prose a harness injects into the model automatically "
                     "and/or `@`-imports other files from. Parsed for `@`-imports.",
                     harnesses, ["context-injection"]), None

    # bin/
    if "bin" in Sl and (execbit or shebang or ext in CODE_EXT or ext == ""):
        a = Asset(relpath, "bin_executable", "code", "high",
                  "Executable in bin/ — added to the Bash tool's PATH while the plugin "
                  "is enabled; can shadow system commands.", harnesses,
                  ["executable", "on-PATH"])
        if shebang:
            a.signals.append(f"shebang:{shebang}")
        return a, None

    # generic runnable code
    if ext in CODE_EXT or shebang or (execbit and not looks_binary(p)):
        if is_exampleish:
            rl, why = "low", "code sample / fixture — reference, not wired to run"
        elif in_component:
            rl, why = "medium", "bundled in a plugin component dir; runs when the owning skill/agent instructs it"
        else:
            rl, why = "medium", "script inside an install surface; runs with the user's privileges"
        a = Asset(relpath, "script", "code", rl,
                  f"Runnable code ({ext or 'no ext'}) — {why}.", harnesses, ["executable-code"])
        if shebang:
            a.signals.append(f"shebang:{shebang}")
        if execbit:
            a.signals.append("executable-bit")
        if low.endswith((".umd.js", ".min.js")) or "vendor" in Sl:
            a.signals.append("vendored-thirdparty")
        return a, None

    # web assets
    if ext in WEB_EXT:
        return Asset(relpath, "web_asset", "web", "medium" if in_component else "low",
                     f"Web asset ({ext}) — served to a browser or rendered; can embed "
                     f"script or pull remote resources.", harnesses, ["web"]), None

    # markdown / text
    if ext in DOC_EXT:
        if in_component or "reference" in Sl:
            return Asset(relpath, "skill_resource", "skill", "low",
                         "Support doc bundled with a skill/agent — loaded into the model "
                         "on demand; same injection surface as SKILL.md.", harnesses,
                         ["loadable-doc"]), None
        return Asset(relpath, "doc", "none", "low",
                     "Markdown/text doc — not auto-loaded by any manifest. Scan only if a "
                     "context file or skill references it.", harnesses, ["doc"]), None

    # structured data
    if ext in DATA_EXT:
        return Asset(relpath, "config", "config", "low",
                     f"Config/data file ({ext}) inside an install surface — may carry "
                     f"paths, URLs, or content a component consumes.", harnesses,
                     ["config"]), None

    # anything else inside a surface: keep it, low, so a reviewer can confirm inert
    if looks_binary(p):
        return None, {"path": relpath, "reason": "binary blob inside an install surface"}
    return Asset(relpath, "other", "config", "low",
                 f"Other file ({ext or 'no ext'}) inside an install surface — included "
                 f"so a reviewer can confirm it is inert.", harnesses, ["uncategorised"]), None


# ─────────────────────────────────────────────────────────────────────────────
# Reference resolution
# ─────────────────────────────────────────────────────────────────────────────

def _subst(tok: str) -> str:
    for v in PATH_VARS:
        tok = tok.replace(v, "")
    # $(git rev-parse --show-toplevel) and friends -> repo root
    tok = re.sub(r"\$\([^)]*\)", "", tok)
    tok = re.sub(r"\$\{[^}]*\}", "", tok)
    return tok.strip().strip('"').strip("'").lstrip("/").lstrip("\\").replace("\\", "/")


def _resolve(cand: str, base_dir: Path, root: Path, extra_anchors=()) -> Path | None:
    cand = _subst(cand)
    if not cand or cand.startswith(("http://", "https://", "npx ", "npm ", "node ", "-")):
        return None
    while cand.startswith("./"):
        cand = cand[2:]
    for anchor in (base_dir, *extra_anchors, root):
        try:
            t = (anchor / cand).resolve()
        except (OSError, ValueError):
            continue
        if t.is_file() and (root == t or root in t.parents):
            return t
    return None


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


KIND_PRIORITY = {
    "script": 0, "config": 0, "other": 0, "doc": 0, "skill_resource": 0, "web_asset": 0,
    "command": 2, "agent": 2, "skill": 2,
    "hook_script": 3, "monitor_script": 3, "mcp_server": 3, "injection_entrypoint": 3,
    "context_file": 3, "hook_config": 3, "mcp_config": 3,
}


def resolve_references(assets: dict, root: Path, blind_spots: list, surfaces=()):
    surf_root = {s.id: s.root for s in surfaces}
    def ensure(relpath, kind, scan_type, relevance, description, harnesses, signals):
        a = assets.get(relpath)
        if a is None:
            a = Asset(relpath, kind, scan_type, relevance, description, harnesses, signals)
            assets[relpath] = a
        else:
            a.bump(relevance)
            a.harnesses |= set(harnesses)
            a.signals.extend(signals)
            if KIND_PRIORITY.get(kind, 0) > KIND_PRIORITY.get(a.kind, 0):
                a.kind, a.scan_type, a.description = kind, scan_type, description
        return a

    for relpath, asset in list(assets.items()):
        if asset.kind not in {"hook_config", "mcp_config", "monitor_config", "settings",
                              "manifest:plugin", "manifest:npm", "manifest:marketplace",
                              "context_file"}:
            continue
        p = root / relpath
        text = read_text(p)
        if text is None:
            continue
        base_dir = p.parent
        # Anchors for ${CLAUDE_PLUGIN_ROOT} and relative component paths:
        #  - the manifest's own dir
        #  - the plugin root (parent of .claude-plugin/, or parent of hooks/)
        #  - every owning install-surface root
        anchors = []
        if base_dir.name == ".claude-plugin":
            anchors.append(base_dir.parent)
        if base_dir.name in ("hooks", "monitors", "agents", "commands", "skills"):
            anchors.append(base_dir.parent)
        for sid in asset.surfaces:
            sr = surf_root.get(sid)
            if sr is not None:
                anchors.append(root / sr if sr else root)
        anchors.append(base_dir)
        plugin_root = anchors[0] if anchors else base_dir
        extra = tuple(dict.fromkeys(anchors))  # dedupe, keep order

        if asset.kind == "context_file":
            for m in re.finditer(r"(?m)^\s*@(\S+)", text):
                t = _resolve(m.group(1), base_dir, root)
                if t:
                    r = rel(t, root)
                    asset.references.add(r)
                    tgt = ensure(r, "skill_resource", "skill", "high",
                                 "File `@`-imported into a context file — its full text is "
                                 "injected into the model automatically.", asset.harnesses,
                                 ["context-injection", "auto-loaded"])
                    tgt.referenced_by.add(relpath)
                else:
                    blind_spots.append(f"{relpath}: @-import '{m.group(1)}' did not resolve")
            continue

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if asset.kind in {"hook_config", "mcp_config", "monitor_config"}:
                blind_spots.append(f"{relpath}: not valid JSON — referenced scripts NOT resolved")
            continue

        # record what a hook manifest actually wires (events + hook types)
        if asset.kind == "hook_config":
            hobj = data.get("hooks", data) if isinstance(data, dict) else {}
            events = [e for e in hobj] if isinstance(hobj, dict) else []
            n_entries = 0
            types = set()
            for ev in events:
                for grp in (hobj.get(ev) or []):
                    for h in (grp.get("hooks") or [grp]):
                        n_entries += 1
                        if isinstance(h, dict) and h.get("type"):
                            types.add(h["type"])
            if events:
                asset.signals.append("events:" + ",".join(sorted(events)))
            asset.signals.append(f"hook_entries:{n_entries}")
            if types:
                asset.signals.append("hook_types:" + ",".join(sorted(types)))
            auto = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                    "PreToolUse", "SubagentStop", "Setup", "sessionStart",
                    "postToolUse", "preToolUse"}
            if set(events) & auto:
                asset.bump("high")
                asset.signals.append("auto-trigger")

        if asset.kind.startswith("manifest"):
            keymap = {
                "hooks": ("hook_config", "wiring", "high"),
                "mcpServers": ("mcp_config", "wiring", "high"),
                "commands": ("command", "skill", "medium"),
                "agents": ("agent", "skill", "medium"),
                "lspServers": ("lsp_config", "wiring", "medium"),
                "contextFileName": ("context_file", "wiring", "high"),
                "main": ("injection_entrypoint", "code", "high"),
            }
            for key, (k, st, rl) in keymap.items():
                if key not in data or isinstance(data[key], dict):
                    continue
                vals = data[key] if isinstance(data[key], list) else [data[key]]
                for cand in vals:
                    if not isinstance(cand, str):
                        continue
                    t = _resolve(cand, base_dir, root, extra_anchors=extra)
                    if t:
                        r = rel(t, root)
                        asset.references.add(r)
                        ensure(r, k, st, rl, f"Referenced by manifest key `{key}` in {relpath}.",
                               asset.harnesses, ["manifest-referenced"]).referenced_by.add(relpath)
                    elif not any((a / _subst(cand)).is_dir() for a in extra):
                        blind_spots.append(f"{relpath}: manifest key '{key}' -> '{cand}' did not resolve")
            if asset.kind == "manifest:npm":
                life = [k for k in ("preinstall", "install", "postinstall", "prepare")
                        if k in data.get("scripts", {})]
                if life:
                    asset.signals.append("npm-lifecycle:" + ",".join(life))
                    asset.bump("high")
                    asset.description += f" Declares lifecycle script(s) {life} that run on `npm install`."
            continue

        # hook / mcp / monitor command strings
        for s in _walk_strings(data):
            if not s or len(s) > 6000:
                continue
            # collapse shell substitutions so a path glued to $(...) stays one token
            s_clean = re.sub(r"\$\([^)]*\)", "", s)
            toks = re.split(r'\s+', s_clean.replace('"', " ").replace("'", " ").strip())
            resolved = []
            for tok in toks:
                if "/" in tok or "\\" in tok or "${" in tok:
                    t = _resolve(tok, base_dir, root, extra_anchors=extra)
                    if t:
                        resolved.append((tok, t))
            for tok, t in resolved:
                r = rel(t, root)
                asset.references.add(r)
                k, desc = {
                    "hook_config": ("hook_script", "Script invoked by a hook — runs on the user's machine when the bound event fires."),
                    "mcp_config": ("mcp_server", "Local MCP server binary/script started when the plugin is enabled."),
                    "monitor_config": ("monitor_script", "Script run as a persistent background process while the plugin is active."),
                    "settings": ("hook_script", "Script invoked by a hook declared in a settings file."),
                }.get(asset.kind, ("script", "Script referenced by a plugin config."))
                ensure(r, k, "code", "high", desc, asset.harnesses,
                       ["config-referenced", "auto-runs"]).referenced_by.add(relpath)
                # wrapper-arg heuristic: "run-hook.cmd session-start" -> sibling file
                for bare in toks:
                    if "/" in bare or "." in bare or bare.startswith("-") or bare in ("[", "]", "||", "&&"):
                        continue
                    for cand_name in (bare, bare + ".sh", bare + ".js", bare + ".py", bare + ".cmd"):
                        cp = t.parent / cand_name
                        if cp.is_file() and (root == cp or root in cp.parents):
                            cr = rel(cp, root)
                            asset.references.add(cr)
                            ensure(cr, "hook_script", "code", "high",
                                   "Script run by a hook wrapper (passed as an argument to the "
                                   "wrapper named in the hook command).", asset.harnesses,
                                   ["wrapper-invoked", "auto-runs"]).referenced_by.add(relpath)
            # flag unresolved path-looking tokens in hook/monitor commands
            if asset.kind in {"hook_config", "monitor_config", "settings"}:
                seen = set()
                for tok in toks:
                    tk = _subst(tok)
                    looks_pathy = tk.count("/") >= 1 and not tk.startswith(("http", "$(", "|", "&"))
                    scripty = ("." in os.path.basename(tk)
                               or any(seg in tk for seg in ("scripts/", "skills/", "hooks/", "bin/")))
                    if looks_pathy and scripty and tk not in seen \
                            and not any(tk == _subst(x) for x, _ in resolved):
                        seen.add(tk)
                        if not _resolve(tok, base_dir, root, extra_anchors=extra):
                            blind_spots.append(
                                f"{relpath}: command references '{tk}' which is not in the repo "
                                f"(runtime-populated, or a dead/incorrect path — a scanner must "
                                f"still account for what runs here)")


# ─────────────────────────────────────────────────────────────────────────────
# Orphan analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_orphans(orphan_paths: list, root: Path):
    related = []
    tops = {}
    execs = []
    exec_by_top = {}
    for rp in orphan_paths:
        top = segs(rp)[0] if segs(rp) else "(root)"
        tops[top] = tops.get(top, 0) + 1
        p = root / rp
        sb = has_shebang(p)
        if sb or (p.suffix.lower() in {".sh", ".bash", ".py", ".rb", ".pl", ".ps1",
                                       ".js", ".cjs", ".mjs", ".ts"}):
            exec_by_top.setdefault(top, []).append({"path": rp, "shebang": sb})
    # cap: show up to 8 per top-dir so the list stays reviewable
    for top, lst in sorted(exec_by_top.items()):
        execs.extend(lst[:8])
        if len(lst) > 8:
            execs.append({"path": f"{top}/… +{len(lst) - 8} more executable files", "shebang": None})

    def present(*names):
        return any((root / n).exists() for n in names)

    if present("extension/manifest.json"):
        related.append({
            "name": "browser extension", "kind": "browser_extension",
            "entrypoint": "extension/manifest.json",
            "why": "MV3 Chrome extension (service worker, content script, <all_urls> host "
                   "permission). Distributed via the Web Store, not the plugin. Scan as a "
                   "browser-extension asset, separately."})
    if present("Cargo.toml") or any("crates/" in o for o in orphan_paths):
        related.append({
            "name": "rust engine", "kind": "native_binary_source",
            "entrypoint": "Cargo.toml / crates/",
            "why": "Rust workspace that builds the `impeccable` binary the skill launcher "
                   "and the hooks exec. The built binary is a separate artifact fetched at "
                   "runtime; scan the release binary + this source separately."})
    if present("cli/bin/cli.js") or any(o.startswith("cli/") for o in orphan_paths):
        related.append({
            "name": "npm CLI wrapper", "kind": "npm_package",
            "entrypoint": "cli/bin/cli.js",
            "why": "npm-distributed launcher for the native binary (package.json `bin`). "
                   "Separate supply chain from the plugin."})
    if any(o.startswith("scripts/lib/transformers/") for o in orphan_paths):
        related.append({
            "name": "plugin build pipeline", "kind": "build_tooling",
            "entrypoint": "scripts/lib/transformers/",
            "why": "Generates the per-harness plugin copies from the source skill. A "
                   "compromise here rewrites every shipped copy — supply-chain surface."})
    return related, dict(sorted(tops.items(), key=lambda kv: -kv[1])), sorted(execs, key=lambda e: e["path"])


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────

def discover(root: Path):
    surfaces, external, snotes = find_surfaces(root)
    surfaces = [s for s in surfaces if s.harness not in EXCLUDED_HARNESSES]

    assets: dict[str, Asset] = {}
    excluded: list[dict] = []
    blind_spots: list[str] = list(snotes)
    orphan_paths: list[str] = []
    total = 0
    surface_files = {s.id: 0 for s in surfaces}

    for fp in iter_files(root):
        if fp.is_symlink():
            tgt = os.readlink(fp)
            excluded.append({"path": rel(fp, root), "reason": f"symlink -> {tgt}"})
            continue
        total += 1
        relpath = rel(fp, root)
        if _excluded_harness_path(relpath):
            excluded.append({"path": relpath, "reason": "out-of-scope harness (hermes/openclaw)"})
            continue
        owning = [s for s in surfaces if s.contains(relpath)]
        if not owning:
            # context files opened directly still matter
            if fp.name.lower() in CONTEXT_FILE_BASENAMES:
                a = Asset(relpath, "context_file", "wiring", "high",
                          "Repo-root context file — a harness loads it when the repo is "
                          "opened directly (not via plugin install).", set(),
                          ["context-injection", "repo-root"])
                assets[relpath] = a
                a.surfaces.add("(repo opened directly)")
            else:
                orphan_paths.append(relpath)
            continue
        harnesses = {s.harness for s in owning if s.harness != "(source template)"}
        a, exc = classify(fp, root, harnesses)
        for s in owning:
            surface_files[s.id] += 1
        if a is not None:
            existing = assets.get(relpath)
            if existing:
                existing.harnesses |= a.harnesses
            else:
                assets[relpath] = a
                existing = a
            existing.surfaces.update(s.id for s in owning)
        elif exc is not None:
            excluded.append(exc)

    resolve_references(assets, root, blind_spots, surfaces)

    all_assets = sorted(assets.values(),
                        key=lambda a: (-RELEVANCE_ORDER[a.relevance], a.scan_type, a.path))
    surface_assets = {s.id: 0 for s in surfaces}
    for a in all_assets:
        for sid in a.surfaces:
            if sid in surface_assets:
                surface_assets[sid] += 1

    related, orphan_tops, orphan_execs = analyse_orphans(orphan_paths, root)

    by_kind, by_scan, by_harness, by_rel = {}, {}, {}, {"high": 0, "medium": 0, "low": 0}
    for a in all_assets:
        by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
        by_scan[a.scan_type] = by_scan.get(a.scan_type, 0) + 1
        by_rel[a.relevance] += 1
        for h in (sorted(a.harnesses) or ["shared"]):
            by_harness[h] = by_harness.get(h, 0) + 1

    return {
        "scanner_version": SCANNER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_path": str(root),
        "repo_name": root.name,
        "install_surfaces": [
            s.to_dict(surface_files[s.id], surface_assets[s.id])
            for s in sorted(surfaces, key=lambda s: (s.kind, s.root))
        ],
        "external_sources": external,
        "summary": {
            "total_files_walked": total,
            "assets_total": len(all_assets),
            "orphan_files": len(orphan_paths),
            "excluded": len(excluded),
            "by_relevance": by_rel,
            "by_scan_type": dict(sorted(by_scan.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_harness": dict(sorted(by_harness.items())),
        },
        "blind_spots": sorted(set(blind_spots)),
        "assets": [a.to_dict() for a in all_assets],
        "orphan_files": {
            "total": len(orphan_paths),
            "note": "not inside any install surface — not copied to a user machine by a "
                    "plugin install / harness load. Executables listed for manual review.",
            "by_top_dir": orphan_tops,
            "executables": orphan_execs,
        },
        "related_out_of_plugin_scope": related,
        "excluded": sorted(excluded, key=lambda e: e["path"]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=None)
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args(argv)

    root = args.repo.expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    result = discover(root)
    out = args.out or Path.cwd() / f"{root.name}.assets.json"
    out.write_text(json.dumps(result, indent=2) + "\n")

    s = result["summary"]
    print(f"[discover_assets {SCANNER_VERSION}] {root.name}", file=sys.stderr)
    print(f"  install surfaces  : {len(result['install_surfaces'])}", file=sys.stderr)
    for sd in result["install_surfaces"]:
        print(f"    - {sd['id']:36} root={sd['root']:20} "
              f"files={sd['files']:4} assets={sd['assets']:4} [{sd['harness']}]", file=sys.stderr)
    print(f"  files walked      : {s['total_files_walked']}", file=sys.stderr)
    print(f"  assets           : {s['assets_total']}  "
          f"(high {s['by_relevance']['high']} / med {s['by_relevance']['medium']} / low {s['by_relevance']['low']})",
          file=sys.stderr)
    print(f"  by scan_type     : {s['by_scan_type']}", file=sys.stderr)
    print(f"  orphan files     : {s['orphan_files']}", file=sys.stderr)
    print(f"  excluded         : {s['excluded']}", file=sys.stderr)
    if result["related_out_of_plugin_scope"]:
        print(f"  related (scan separately):", file=sys.stderr)
        for r in result["related_out_of_plugin_scope"]:
            print(f"    - {r['name']} ({r['entrypoint']})", file=sys.stderr)
    if result["blind_spots"]:
        print(f"  BLIND SPOTS ({len(result['blind_spots'])}):", file=sys.stderr)
        for b in result["blind_spots"]:
            print(f"    - {b}", file=sys.stderr)
    print(f"  -> {out}", file=sys.stderr)
    if args.stdout:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
