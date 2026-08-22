#!/usr/bin/env python3
"""mcp-repo-seeds/registry.json -- the MCP pipeline's tracking file, same
role as ../registry.py plays for the skills pipeline (see
PROPOSED_PIPELINE.md's side-by-side table for why this is a *separate*
file/schema rather than a shared one -- the two pipelines share a pattern,
not state).

**One row per unique server, `sources` is a list, overlap is expected.**
Same idea as ../registry.py: the official registry, Glama, and the
awesome-mcp-servers seed list routinely surface the same server (the seed
list's own README says it's synced with Glama) -- that's corroborating
signal, not a duplicate to collapse away, so every row keeps a `sources`
list of every channel that ever surfaced it instead of overwriting.

**Identity key**: prefer the server's GitHub repo (`github:<owner>/<repo>`,
lowercased) when one can be resolved -- this is what lets all three sources
dedupe onto one row for the same server. Falls back to a source-scoped id
(`official:<name>`, `glama:<slug>`) for entries with no resolvable GitHub
link (e.g. a closed-source remote-only server) -- these can't be deduped
across sources without a repo to key on, which is a real, documented
limitation, not a bug.

Every entry also carries `errors`: a list of {date, source, message}, never
overwritten -- a failed fetch/scan is itself useful information (this repo
is gone, this manifest 404s), so it's recorded rather than silently
dropped. `status` is "active" (default) or "error" (every fetch attempt for
this entry has failed so far -- still kept, not removed, since the source
list still names it).

Use this module's functions -- don't hand-edit registry.json directly.
"""

import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GITHUB_REPO_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")

MCP_DIR = Path(__file__).parent
REPO_SEEDS_DIR = MCP_DIR.parent / "mcp-repo-seeds"
REGISTRY_FILE = REPO_SEEDS_DIR / "registry.json"
SEED_LISTS_FILE = REPO_SEEDS_DIR / "repo_seeds.json"
RAW_DIR = MCP_DIR.parent / "mcp-search-raw"
README_DIR = RAW_DIR / "readmes"

VALID_SOURCES = {"official_registry", "glama", "awesome-mcp-servers"}


def parse_github_repo_url(url: str) -> tuple[str, str] | None:
    match = GITHUB_REPO_RE.search(url or "")
    if not match:
        return None
    return match.group(1), match.group(2).rstrip(".")


def make_id(repo_url: str | None, source_type: str, source_key: str) -> str:
    """Identity key for a registry row -- see module docstring."""
    owner_repo = parse_github_repo_url(repo_url) if repo_url else None
    if owner_repo:
        owner, repo = owner_repo
        return f"github:{owner.lower()}/{repo.lower()}"
    return f"{source_type}:{source_key}"


def readme_path_for(repo_url: str) -> Path:
    owner, repo = parse_github_repo_url(repo_url)
    return README_DIR / f"{owner}__{repo}.md"


# Lines to skip outright while hunting for the README's first real
# paragraph -- markdown badges/images, horizontal rules, table rows, and
# the MCP registry's own `mcp-name: io.github.owner/repo` marker convention
# (used by scan_mcp.py/server.json-adjacent tooling to pin a README to its
# registry identity -- confirmed appearing two ways in real READMEs in this
# corpus: HTML-comment-wrapped, e.g. openzim-mcp's
# `<!-- mcp-name: io.github.cameronrye/openzim-mcp -->` -- already invisible
# to this function since HTML comments get consumed by the tag-strip pass
# below -- and completely bare, e.g. a drone-governance MCP server's README
# with a plain `mcp-name: io.github.CSOAI-ORG/...` line sitting between a
# badge and a heading. The bare form has no markup at all to distinguish it
# from a real sentence without this explicit check -- found by hand
# reviewing this function's output against 5 random real READMEs). Checked
# on the RAW (pre-HTML-strip) line: markdown badges/images are markdown
# syntax, not HTML, so a line like [![CI](...)](...) contains no HTML tags
# at all and would otherwise survive the tag-stripping pass unchanged.
_README_SKIP_RE = re.compile(r"^\s*(?:\[!\[.*|!\[.*|[-*_]{3,}|\|.*\|)\s*$")
_MCP_NAME_MARKER_RE = re.compile(r"^mcp-name:\s*\S+\s*$")

# Centered logo/badge/title blocks wrapped in raw HTML (<p align="center">,
# <h1 align="center">Title</h1>, ...) are common at the top of MCP server
# READMEs (openzim-mcp's is a real example -- see
# test-data/openzim-mcp-cluster/). An HTML heading tag needs its own check:
# it doesn't start with "#" (the markdown-heading test below), and unlike a
# bare wrapper tag (<p align="center"> alone on its line, which the
# tag-strip pass reduces to "" and skips harmlessly) it carries real visible
# text between open/close tags on one line -- <h1 align="center">OpenZIM MCP
# Server</h1> strips down to "OpenZIM MCP Server", which without this check
# reads exactly like a real sentence and gets kept as the "description."
_HTML_HEADING_RE = re.compile(r"<h[1-6][\s>]", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
README_DESCRIPTION_MAX_CHARS = 600


def extract_readme_description(readme_text: str) -> str | None:
    """Heuristic "what does this README say about itself" extraction: the
    first paragraph of real prose, skipping badge/image/HTML-wrapper noise
    and heading lines (markdown `#`/HTML `<h1-6>` alike). Deliberately
    simple -- a first-paragraph heuristic, not a summarizer -- matching
    this pipeline's documented stance (MCP_PIPELINE.md's "Known gaps")
    against building a fancier README-summarization step without more
    hand-reviewed cases to design one against. This exists to make "what
    does the README's own opening line say" comparable to the description
    each source reported, not to replace either.

    None if no paragraph-shaped content is found (empty/badges-only readme).
    Truncated at README_DESCRIPTION_MAX_CHARS with a trailing ellipsis --
    this is a comparison snippet, not a full capture."""
    if not readme_text:
        return None

    lines = readme_text.splitlines()
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break  # blank line after real content -> paragraph is done
            continue  # blank line before any content -> keep skipping

        is_heading = stripped.startswith("#") or _HTML_HEADING_RE.search(stripped)
        if is_heading:
            if paragraph:
                break  # a heading after real content ends the paragraph
            continue  # heading before any content (e.g. the H1 title) -> skip

        if _README_SKIP_RE.match(stripped) or _MCP_NAME_MARKER_RE.match(stripped):
            continue  # markdown badge/image/hr/table noise, or a bare mcp-name: marker

        visible = _HTML_TAG_RE.sub("", stripped).strip()
        if not visible:
            continue  # pure HTML wrapper line (<p align="center">, </p>, a bare <img>, ...) -- no visible text
        paragraph.append(visible)

    if not paragraph:
        return None
    text = " ".join(paragraph)
    if len(text) > README_DESCRIPTION_MAX_CHARS:
        # Truncate to leave room for the "..." suffix itself -- the naive
        # version (slice to the full cap, then append "...") guarantees
        # only "<=cap chars of content", not "<=cap chars total," so the
        # actual stored value could run 1-3 chars over the cap depending on
        # where the trailing word-boundary trim lands. Found by hand
        # stress-testing this exact function against a no-early-whitespace
        # pathological input (603 chars back instead of the intended 600).
        budget = README_DESCRIPTION_MAX_CHARS - 3
        text = text[:budget].rsplit(" ", 1)[0] + "..."
    return text


def load_registry() -> list[dict]:
    """Empty list only for a genuinely-not-yet-created registry.json. A file
    that EXISTS but fails to parse raises, deliberately -- confirmed the
    hard way that the alternative (silently treating it as "empty, start
    fresh") is actively dangerous: a supervised job killed mid-write once
    left a real, 82K-row registry.json truncated to 0 bytes (see
    save_registry()'s docstring for the fix to the write side of this).
    Swallowing that here would mean load_registry()->save_registry() call
    sequences downstream quietly overwrite the truncated file with an
    equally-empty one instead of ever surfacing that anything was lost --
    see rebuild_registry_from_raw.py for the actual recovery path when this
    fires for real."""
    if not REGISTRY_FILE.exists():
        return []
    text = REGISTRY_FILE.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{REGISTRY_FILE} exists ({len(text)} bytes) but isn't valid JSON -- NOT treating this as "
            f"'no registry yet' (that would silently paper over real data loss). If this file was just "
            f"truncated/corrupted, see rebuild_registry_from_raw.py to rebuild from the cached raw pull "
            f"dumps rather than starting over from an empty registry."
        ) from e


def save_registry(registry: list[dict]) -> None:
    """Atomic write (temp file + os.replace), not a direct write_text() to
    REGISTRY_FILE -- found the hard way why this matters: write_text()
    truncates the target file before writing its new content, with no
    signal handler guarding that window, so a process killed mid-save
    (e.g. `kill`/SIGTERM landing between the truncate and the write
    completing -- confirmed via `supervise.sh stop` on a long-running
    fetch_mcp_rankings.py job) can leave REGISTRY_FILE completely empty --
    82K rows, gone, in the time it takes a signal to arrive. os.replace()
    is atomic on the same filesystem: the temp file is fully written and
    flushed first, so REGISTRY_FILE is always either the old complete
    version or the new complete version, never a partial one, regardless
    of when a kill signal arrives."""
    REPO_SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    registry = sorted(registry, key=lambda r: r["id"])
    tmp_path = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(registry, indent=2) + "\n")
    tmp_path.replace(REGISTRY_FILE)


def find(registry: list[dict], entry_id: str, index: dict[str, dict] | None = None) -> dict | None:
    """`index`, if given (see build_index()), makes this O(1) instead of an
    O(n) linear scan -- use it for any loop calling find()/upsert() more
    than a handful of times (bulk pulls at 10k-100k scale turn the default
    linear scan into O(n^2) otherwise, which is exactly what made the first
    100k-scale glama/official-registry pull crawl)."""
    if index is not None:
        return index.get(entry_id)
    for r in registry:
        if r["id"] == entry_id:
            return r
    return None


def build_index(registry: list[dict]) -> dict[str, dict]:
    """id -> row lookup. Build once per script run before a bulk upsert
    loop and pass it to every find()/upsert() call in that loop (see
    pull_official_registry.py, pull_glama.py, pull_seed_repo.py)."""
    return {r["id"]: r for r in registry}


def source_types(entry: dict) -> set[str]:
    return {s["type"] for s in entry.get("sources", [])}


def get_source(entry: dict, source_type: str) -> dict | None:
    return next((s for s in entry.get("sources", []) if s["type"] == source_type), None)


def upsert(registry: list[dict], entry: dict, index: dict[str, dict] | None = None) -> dict:
    """Insert or update a row, ADDING a source descriptor rather than
    overwriting provenance -- same contract as ../registry.py's upsert().

    `entry` shape: {"repo_url", "source", "name", "description"?, ...
    source-type-specific detail fields}. `id` is derived via make_id() using
    `entry.get("source_key")` (required when repo_url can't resolve a
    GitHub owner/repo -- e.g. the official-registry server `name`, or a
    Glama `slug`) as the fallback identity.

    `name`/`description`/`repo_url` on the row itself are filled in on
    first insert and then only overwritten when the existing value is empty
    -- later sources corroborate, they don't clobber an earlier source's
    (possibly better) description. Glama's data is the one documented
    exception: PROPOSED_PIPELINE.md's source notes say to prefer Glama's
    description/attributes over self-derived versions, so a "glama" source
    is allowed to overwrite `description`.

    `description`, unlike `name`/`repo_url`, is ALSO left on the source
    descriptor itself (not popped before building it) -- a real gap found
    by hand: this row-level merge is genuinely lossy (Glama sometimes just
    echoes the README's own tagline verbatim, sometimes synthesizes a
    richer summary, sometimes has none at all -- see
    fetch_mcp_security.py-era test_e2e_pipeline.py's description-vs-readme
    comparison), and reviewing "what did each source actually say" used to
    require re-opening the raw mcp-search-raw/{official_registry,glama}.json
    dump and manually matching by id -- annoying, and only possible while
    that raw dump still exists on disk. Keeping description per-source
    means `sources[].description` always answers that directly from
    registry.json alone, same as every other source-specific field
    (registry_type, package_identifier, ...).

    Pass `index` (see build_index()) when calling this in a loop -- without
    it, the existing-row lookup is an O(n) scan of `registry`, so a bulk
    pull of thousands of entries degrades to O(n^2).
    """
    if entry["source"] not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {entry['source']!r}")

    entry = dict(entry)
    source_type = entry.pop("source")
    repo_url = entry.pop("repo_url", None)
    source_key = entry.pop("source_key", None)
    name = entry.pop("name", None)
    description = entry.get("description")  # NOT popped -- see docstring; stays in `entry` -> descriptor below

    entry_id = make_id(repo_url, source_type, source_key)
    # whatever's left is source-type-specific detail
    descriptor = {"type": source_type, "added": datetime.date.today().isoformat(), **entry}

    existing = find(registry, entry_id, index=index)
    if existing:
        existing_descriptor = next((s for s in existing["sources"] if s["type"] == source_type), None)
        if existing_descriptor:
            existing_descriptor.update({k: v for k, v in descriptor.items() if k != "added"})
        else:
            existing["sources"].append(descriptor)
        if repo_url and not existing.get("repo_url"):
            existing["repo_url"] = repo_url
        if name and not existing.get("name"):
            existing["name"] = name
        if description and (not existing.get("description") or source_type == "glama"):
            existing["description"] = description
        existing["status"] = "active"
        return existing

    row = {
        "id": entry_id,
        "name": name,
        "description": description,
        "repo_url": repo_url,
        "added": datetime.date.today().isoformat(),
        "status": "active",
        "sources": [descriptor],
        "errors": [],
    }
    registry.append(row)
    if index is not None:
        index[entry_id] = row
    return row


def record_error(
    registry: list[dict], entry_id: str, source: str, message: str, index: dict[str, dict] | None = None
) -> dict | None:
    """Append an error without removing the entry. If the entry has never
    successfully carried any data (no repo_url/name yet, no readme), mark it
    status="error" -- still kept for visibility, not deleted. Pass `index`
    (build_index()) in a loop over many entries -- same O(n^2) concern as
    upsert()."""
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry["errors"].append({
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "message": message,
    })
    return entry


def mark_readme(
    registry: list[dict], entry_id: str, path: Path, source: str, index: dict[str, dict] | None = None
) -> dict | None:
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry["readme_path"] = str(path.relative_to(MCP_DIR.parent))
    entry["readme_source"] = source
    entry["readme_fetched"] = datetime.datetime.now().isoformat(timespec="seconds")
    return entry


def set_stars(
    registry: list[dict], entry_id: str, stars: int, index: dict[str, dict] | None = None,
    language: str | None = None,
) -> dict | None:
    """Record a GitHub stargazer count fetched by fetch_mcp_rankings.py, plus
    when it was fetched -- same shape as ../registry.py's set_stars() for the
    skills pipeline. No-ops (returns None) if entry_id isn't in the
    registry.

    `language` is GitHub's own repo-level primary-language detection
    (`GET /repos/{owner}/{repo}`'s `language` field) -- captured here rather
    than in its own fetch pass because it's already present, for free, in
    the exact same API response this function's caller (fetch_stars()) is
    already making for the star count; a separate fetch would double the
    GitHub request budget for no reason. Only written when given (not
    overwritten with None on every star refresh) so a value already
    resolved by a richer future source isn't blown away by a plain refresh."""
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry["stars"] = stars
    entry["stars_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    if language is not None:
        entry["language"] = language
    return entry


def set_security_scan(
    registry: list[dict], entry_id: str, scan: dict, index: dict[str, dict] | None = None
) -> dict | None:
    """Record OSV.dev vulnerability-scan results fetched by
    fetch_mcp_security.py. `scan` keys land directly on the row:
    security_vuln_count (int), security_vuln_ids (list[str], e.g.
    ["GHSA-...", "PYSEC-..."]), security_max_severity (one of "CRITICAL"/
    "HIGH"/"MODERATE"/"LOW", or absent if OSV had no severity label for any
    finding). A package with zero known vulnerabilities is a real,
    meaningful result (not a fetch failure) -- callers should pass
    {"security_vuln_count": 0, "security_vuln_ids": []} for that case, not
    skip the call, so "field present" always means "this package's package
    manager registry was actually checked," not "checking was attempted."
    """
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry.update(scan)
    entry["security_source"] = "osv"
    entry["security_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    return entry


def set_npm_ranking(
    registry: list[dict], entry_id: str, ranking: dict, index: dict[str, dict] | None = None
) -> dict | None:
    """Record npm ranking signal fetched by fetch_mcp_rankings.py. `ranking`
    keys land directly on the row (weekly_downloads, monthly_downloads,
    npm_dependents, npm_score_final/quality/popularity/maintenance -- see
    fetch_mcp_rankings.py's fetch_npm_search/fetch_npm_point for which keys
    are actually present for a given row). Only keys npm's API actually
    reported are included by the caller -- a field's absence means "this
    source didn't report it," not "it's zero," so this never writes a field
    as null just to keep the schema uniform.

    `downloads_source` is fixed at "npm" for now (the only download source
    this pipeline fetches) but kept as an explicit field rather than assumed,
    since a server could in principle draw this from more than one registry
    later (pypi, say)."""
    entry = find(registry, entry_id, index=index)
    if entry is None:
        return None
    entry.update(ranking)
    entry["downloads_source"] = "npm"
    entry["downloads_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    return entry


# --- seed list tracking (mcp-repo-seeds/repo_seeds.json) -- mirrors
# ../registry.py's repo_seeds.json exactly, just pointed at a separate file. ---

def load_seed_lists() -> list[dict]:
    if not SEED_LISTS_FILE.exists():
        return []
    return json.loads(SEED_LISTS_FILE.read_text())


def save_seed_lists(seed_lists: list[dict]) -> None:
    REPO_SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    SEED_LISTS_FILE.write_text(json.dumps(seed_lists, indent=2) + "\n")


def mark_seed_pulled(name: str, upstream_repo: str, vendored_path: str) -> dict:
    seed_lists = load_seed_lists()
    entry = next((s for s in seed_lists if s["name"] == name), None)
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if entry is None:
        entry = {"name": name, "upstream_repo": upstream_repo, "vendored_path": vendored_path, "last_pulled": now}
        seed_lists.append(entry)
    else:
        entry["last_pulled"] = now
    save_seed_lists(seed_lists)
    return entry
