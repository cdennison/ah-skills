"""Detect "junk aggregator" repos: repos whose content is just other
people's skills copied verbatim into a data dump, rather than an original
source of skills.

Two independent signals, either one is sufficient:

1. Structural: the skill's path embeds a *second* owner/repo pair past a
   known dump-directory name, e.g.
   "NeverSight/learn-skills.dev/data/skills-md/amanning3390/hermeshub/agent-hardening/SKILL.md"
   -- "skills-md" followed by another owner ("amanning3390") and repo
   ("hermeshub") before the skill itself. A real source repo's skills live
   at "owner/repo/skills/<name>/SKILL.md" or similar, not with a second
   owner/repo pair nested inside.

2. Statistical: a repo where most of its rows' SKILL.md content is a
   content-hash duplicate of a skill that also exists under a *different*
   GitHub owner elsewhere in the corpus (parsed from the CSV's `also_in`
   column). This specifically means "this repo's skills match other
   people's repos" -- not "this repo republishes its own content at
   multiple install paths" and not "this repo got renamed on GitHub and
   both URLs got crawled," both of which inflate raw `duplicate_count`
   without being aggregation at all.

   Earlier version of this check used raw `duplicate_count > 1` as "is a
   duplicate," which is wrong: duplicate_count counts *all* locations
   sharing that content hash, including the row's own repo under a prior
   name. That version false-positived at 100% on affaan-m/ECC (crawled
   under both `ECC` and its rename `everything-claude-code`) and at 97% on
   sickn33/agentic-awesome-skills (duplicates were almost entirely *within*
   that one repo, at different paths, not sourced from elsewhere).
   Restricting to cross-owner matches only drops both false positives to
   ~0% while still catching real aggregators: e.g. witt3rd/oh-my-hermes
   comes out 10/10 (100%) cross-owner-duplicate, matching skills that also
   exist under their original owners elsewhere in the corpus.
"""

from __future__ import annotations

from collections import defaultdict

# Directory names conventionally used by scrape/aggregation tooling to dump
# other repos' skill files.
DUMP_DIR_NAMES = {
    "skills-md",
    "skills_md",
    "scraped",
    "scraped-skills",
    "scrape",
    "mirror",
    "mirrors",
    "dump",
    "imported",
}

# Repo is flagged statistically if it has at least this many rows and this
# fraction of them content-match a *different* owner's repo elsewhere in
# the corpus.
MIN_ROWS_FOR_STATS = 5
CROSS_OWNER_DUPLICATE_FRACTION_THRESHOLD = 0.8


def is_likely_aggregator_path(path: str, owner: str = "", repo: str = "") -> tuple[bool, str]:
    """Structural check: does `path` embed a second owner/repo pair past a
    known dump-directory name? Returns (flagged, evidence)."""
    parts = [p for p in (path or "").split("/") if p]
    if owner and repo and len(parts) >= 2 and parts[0] == owner and parts[1] == repo:
        rest = parts[2:]
    else:
        rest = parts

    for i, seg in enumerate(rest[:-1]):
        if seg.lower() in DUMP_DIR_NAMES:
            remaining = rest[i + 1 :]
            # need at least <nested-owner>/<nested-repo>/.../SKILL.md
            if len(remaining) >= 3:
                return True, (
                    f"path embeds a second owner/repo past dump dir '{seg}': "
                    f"{'/'.join(remaining[:2])}"
                )
    return False, ""


def _other_owners(also_in: str, self_owner: str) -> set[str]:
    """Parse the CSV's `also_in` column (comma-separated
    "owner/repo:owner/repo/path/SKILL.md" entries) and return every owner
    named that isn't `self_owner`."""
    owners: set[str] = set()
    for entry in (also_in or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        owner_repo = entry.split(":", 1)[0].strip()
        owner = owner_repo.split("/", 1)[0]
        if owner and owner != self_owner:
            owners.add(owner)
    return owners


def find_statistical_aggregators(
    rows: list[dict],
    owner_key: str = "owner",
    repo_key: str = "repo",
    also_in_key: str = "also_in",
) -> dict[tuple[str, str], str]:
    """Given all CSV rows, return {(owner, repo): evidence} for repos where
    most rows content-match a *different* owner's repo elsewhere in the
    corpus (i.e. this repo's skills are copies of other people's skills,
    not self-republished content)."""
    by_repo: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (row.get(owner_key, ""), row.get(repo_key, ""))
        by_repo[key].append(row)

    flagged: dict[tuple[str, str], str] = {}
    for key, repo_rows in by_repo.items():
        if len(repo_rows) < MIN_ROWS_FOR_STATS:
            continue
        owner = key[0]
        cross_owner_count = sum(
            1 for row in repo_rows if _other_owners(row.get(also_in_key, ""), owner)
        )
        fraction = cross_owner_count / len(repo_rows)
        if fraction >= CROSS_OWNER_DUPLICATE_FRACTION_THRESHOLD:
            flagged[key] = (
                f"{cross_owner_count}/{len(repo_rows)} rows ({fraction:.0%}) content-match "
                f"a different GitHub owner's repo elsewhere in the corpus"
            )
    return flagged


def is_aggregator_row(
    row: dict,
    statistical_aggregators: dict[tuple[str, str], str] | None = None,
    path_key: str = "path",
    owner_key: str = "owner",
    repo_key: str = "repo",
) -> tuple[bool, str]:
    """Combined check for a single CSV row (dict-like, e.g. csv.DictReader
    row). Structural check first (cheap, per-row); statistical check needs
    the precomputed `statistical_aggregators` map from
    find_statistical_aggregators() run once over the whole file."""
    path = row.get(path_key, "")
    owner = row.get(owner_key, "")
    repo = row.get(repo_key, "")

    flagged, evidence = is_likely_aggregator_path(path, owner, repo)
    if flagged:
        return True, evidence

    if statistical_aggregators is not None:
        stat_evidence = statistical_aggregators.get((owner, repo))
        if stat_evidence:
            return True, stat_evidence

    return False, ""
