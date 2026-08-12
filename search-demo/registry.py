#!/usr/bin/env python3
"""repo-seeds/registry.json -- the single source of truth for every repo fed
into the RAG pipeline. clone_repos.py reads only this file.

**One registry row per repo -- `sources` is a list, and overlap is expected
and welcome.** The same repo routinely gets discovered more than once: it
might be in the vendored awesome-list AND turn up in a `search_github.py`
query AND be listed in the Claude plugin marketplace. That's not a
duplicate or a conflict to resolve -- it's useful signal (a repo three
independent sources agree on is probably a good one), so every entry keeps
a `sources` array recording *every* channel that ever surfaced it, instead
of overwriting to just the most recent one. `upsert()` appends a new source
descriptor when a repo is rediscovered through a different channel, or
updates the existing descriptor's detail (e.g. a re-run search's date) when
rediscovered through the *same* channel -- it never discards a prior
source. `source_types(entry)` gives the set of channels that found a repo;
`r["sources"][0]["type"]` is whichever channel found it first.

Each source descriptor's `type` is one of:

  "<seed name>" -- "seed" is an architectural concept, not a literal type
                   string: it means "came from an upstream awesome-list,
                   vendored wholesale and synced via sync_seeds()." The
                   `type` actually stored is always that specific list's
                   name (e.g. "officialskills.sh" for
                   repo-seeds/awesome-agent-skills/README.md), never the
                   word "seed" itself -- see SEED_FILES below. This is what
                   lets two different vendored lists both feed the registry
                   without their descriptors colliding under one generic
                   label.
  "search"      -- came from search_github.py, approved by a human after
                   review; keeps the query/sort/exact used, review date, and
                   the result's 1-based `rank` (position in that search)
  "manual"      -- someone added it by hand; `note` records why
  "marketplace" -- came from fetch_marketplace.py (Anthropic's official
                   Claude plugin marketplace listing); keeps the plugin
                   name it was found under
  "skills.sh"   -- came from add_skillsh_leaderboard.py, parsed out of a
                   leaderboard-raw/combined.json snapshot (see
                   pull_leaderboard.py); keeps the best (lowest) rank and
                   the count of leaderboard skills seen from that repo

This is repo-level bookkeeping only. It has no effect on how many times a
repo gets cloned or a skill gets indexed: `clone_repos.py` still clones each
`owner/repo` exactly once regardless of how many sources list it (the
registry has one row per repo, full stop), and `extract_search_raw.py` /
`index_qdrant.py` still produce exactly one point per `SKILL.md` path found
on disk. Multi-source tracking answers "where did we hear about this repo
from," not "how many times is this skill indexed" -- the latter is always
once.

Every entry also has a `status`, default "active":

  status == "active" -- included in the pipeline (the default)
  status == "skip"    -- marked to exclude, with a required `skip_reason`

IMPORTANT: `status: "skip"` is currently schema-only / informational.
clone_repos.py's repo_pairs() does NOT filter on it yet, so marking a repo
skipped has no effect on cloning, extraction, or indexing today -- it just
records the decision (e.g. from user feedback: "too noisy", "not actually
skills, just a README mentioning the word") for a human to see and for a
future pipeline change to act on. See DAILY_JOB.md for the intended future
behavior once this is wired up.

Repos are never hard-deleted from the registry by the daily review process
-- use `skip`/`unskip` for that. `remove` still exists as an escape hatch
for outright mistakes (e.g. a typo'd owner/repo that was never real), not
for "this turned out to be low quality."

Use this module's functions (or the CLI below) to curate the list -- don't
hand-edit registry.json directly, so every entry stays well-formed.

Each entry also carries `last_synced` -- an ISO timestamp meaning "as of this
time, the repo was cloned on disk AND had run through extract+index (RAG)".
For a small/routine run, it's stamped by `mark-synced` (archived/run_pipeline.sh
calls this as its last step, after extract_search_raw.py + index_qdrant.py
both succeed) on every repo that has a directory under repos/ at that point.
For a large/batched run, batch_pipeline.py stamps it per-batch via
mark_synced_pairs() instead (see that function's docstring for why -- repos/
only ever holds the current batch there, so the disk-presence check
mark_synced_from_disk() uses would misclassify prior batches as failures).
A repo missing `last_synced` entirely has never made it through a full
pipeline run; one whose `last_synced` date isn't today hasn't been
refreshed today (run `./registry.py unsynced` to see the list).

CLI usage:
    ./registry.py add-manual owner/repo "reason this was added"
    ./registry.py add-search results.json --approve owner/repo --approve owner2/repo2
    ./registry.py sync-seed          # additive: pick up new repos from every list in SEED_FILES, never touches existing entries
    ./registry.py seeds              # list every vendored seed list + when its on-disk copy was last pulled (repo-seeds/repo_seeds.json)
    ./registry.py mark-seed-pulled officialskills.sh   # stamp last_pulled=now after manually refreshing a seed list's vendored copy
    ./registry.py skip owner/repo "reason"
    ./registry.py unskip owner/repo
    ./registry.py mark-synced        # stamp last_synced=now on every repo present in repos/ (run by archived/run_pipeline.sh; batch_pipeline.py does its own per-batch equivalent)
    ./registry.py unsynced           # list repos not synced today (never synced, or stale)
    ./registry.py list [--source search|officialskills.sh|manual] [--status active|skip]
    ./registry.py remove owner/repo   # hard delete -- only for actual mistakes, not quality judgments
    ./registry.py update-skillsh owner/repo --rank N --skill-count N --top-installs N
                                      # refresh skills.sh ranking fields for one repo, independent of
                                      # clone/index state (works even if repos/<owner>/<repo> is gone)
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

REGISTRY_FILE = Path(__file__).parent / "repo-seeds" / "registry.json"
REPOS_DIR = Path(__file__).parent / "repos"

# repo-seeds/repo_seeds.json -- NOT the same thing as registry.json. This
# tracks the vendored seed *lists themselves* (upstream repo, where they're
# vendored to on disk, and when that vendored copy was last refreshed) so
# staleness of the source lists is visible, separate from staleness of any
# individual discovered repo (last_synced, in registry.json). Re-vendoring a
# seed list (copying its files from upstream) is a manual/periodic step --
# mark_seed_pulled() just records when that last happened.
REPO_SEEDS_FILE = Path(__file__).parent / "repo-seeds" / "repo_seeds.json"

# Every upstream awesome-list vendored wholesale into repo-seeds/ and synced
# via sync_seeds(). "seed" describes the *mechanism* (regex-scrape every
# github.com link out of a vendored file, additive, never destructive) --
# each list still gets its own `name`, which is what actually gets written
# as the source descriptor's `type` in registry.json. Add an entry here to
# start tracking another vendored list; sync_seeds() loops over all of them.
SEED_FILES = [
    {
        "name": "officialskills.sh",
        "path": Path(__file__).parent / "repo-seeds" / "awesome-agent-skills" / "README.md",
        "file": "awesome-agent-skills/README.md",
    },
]

VALID_SOURCES = {"search", "manual", "marketplace", "skills.sh"} | {sf["name"] for sf in SEED_FILES}


def load_registry() -> list[dict]:
    if not REGISTRY_FILE.exists():
        return []
    return json.loads(REGISTRY_FILE.read_text())


def save_registry(registry: list[dict]) -> None:
    registry = sorted(registry, key=lambda r: (r["owner"].lower(), r["repo"].lower()))
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2) + "\n")


def find(registry: list[dict], owner: str, repo: str) -> dict | None:
    for r in registry:
        if r["owner"].lower() == owner.lower() and r["repo"].lower() == repo.lower():
            return r
    return None


def source_types(entry: dict) -> set[str]:
    """Every channel that has ever surfaced this repo."""
    return {s["type"] for s in entry.get("sources", [])}


def upsert(registry: list[dict], entry: dict) -> dict:
    """Insert or update a registry entry, ADDING a source descriptor rather
    than overwriting the repo's provenance. `entry` takes the same shape
    call sites have always used -- {"owner", "repo", "source": <type>, ...
    type-specific detail fields} -- and gets split into the row's top-level
    identity fields (owner/repo/url/added/status) plus a `sources` list
    entry built from "source" + whatever detail fields were passed.

    If this repo already has a descriptor of the same type, that descriptor
    is updated in place (e.g. re-running a search updates the date/query on
    the existing "search" descriptor rather than adding a second one). If
    it has descriptors of *other* types, they are left untouched -- this is
    what makes rediscovering a repo through a new channel additive instead
    of destructive.
    """
    if entry["source"] not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {entry['source']!r}")

    entry = dict(entry)
    owner, repo = entry.pop("owner"), entry.pop("repo")
    source_type = entry.pop("source")
    url = entry.pop("url", f"https://github.com/{owner}/{repo}")
    status = entry.pop("status", None)
    added = entry.pop("added", None)
    # whatever's left in `entry` is source-type-specific detail (seed_file,
    # search_query/sort/exact/reviewed_date, note, marketplace_plugin, ...)
    descriptor = {"type": source_type, "added": datetime.date.today().isoformat(), **entry}

    existing = find(registry, owner, repo)
    if existing:
        existing_descriptor = next((s for s in existing["sources"] if s["type"] == source_type), None)
        if existing_descriptor:
            existing_descriptor.update({k: v for k, v in descriptor.items() if k != "added"})
        else:
            existing["sources"].append(descriptor)
        if status:
            existing["status"] = status
        return existing

    row = {
        "owner": owner,
        "repo": repo,
        "url": url,
        "added": added or datetime.date.today().isoformat(),
        "status": status or "active",
        "sources": [descriptor],
    }
    registry.append(row)
    return row


def update_skillsh(owner: str, repo: str, rank: int | None = None, skill_count: int | None = None,
                    top_installs: int | None = None) -> dict:
    """Refresh the `skills.sh` source descriptor's rank/skill_count/top_installs
    on an already-tracked repo -- e.g. after a fresh leaderboard pull, or a
    manual correction -- without touching clone/extract/index state. Only
    fields given are updated; omitted ones keep whatever was there before.
    Works even if the repo's directory under repos/ has been deleted, since
    this only ever writes to registry.json.
    """
    if rank is None and skill_count is None and top_installs is None:
        raise ValueError("update_skillsh requires at least one of rank, skill_count, top_installs")
    registry = load_registry()
    detail = {"rank_last_updated": datetime.date.today().isoformat()}
    if rank is not None:
        detail["rank"] = rank
    if skill_count is not None:
        detail["skill_count"] = skill_count
    if top_installs is not None:
        detail["top_installs"] = top_installs
    entry = upsert(registry, {"owner": owner, "repo": repo, "source": "skills.sh", **detail})
    save_registry(registry)
    return entry


def add_manual(owner: str, repo: str, note: str) -> dict:
    if not note or not note.strip():
        raise ValueError("a manual entry requires a non-empty note explaining why it was added")
    registry = load_registry()
    entry = upsert(registry, {"owner": owner, "repo": repo, "source": "manual", "note": note.strip()})
    save_registry(registry)
    return entry


def add_search_results(results_json_path: Path, approved_full_names: list[str]) -> list[dict]:
    """Read a search_github.py --format json output file and copy the approved
    (owner/repo full_name) entries into the registry as source="search",
    carrying over the query/sort/exact used to find them."""
    data = json.loads(Path(results_json_path).read_text())
    query, sort, exact = data["query"], data["sort"], data["exact"]

    by_full_name = {r["full_name"]: r for r in data["results"]}
    registry = load_registry()
    added = []
    today = datetime.date.today().isoformat()
    for full_name in approved_full_names:
        result = by_full_name.get(full_name)
        if not result:
            print(f"[warn] {full_name} not found in {results_json_path}, skipping", file=sys.stderr)
            continue
        entry = upsert(
            registry,
            {
                "owner": result["owner"],
                "repo": result["repo"],
                "source": "search",
                "query": query,
                "sort": sort,
                "exact": exact,
                "reviewed_date": today,
                "rank": result.get("rank"),
                "rank_last_updated": today,
            },
        )
        added.append(entry)
    save_registry(registry)
    return added


def skip(owner: str, repo: str, reason: str) -> dict:
    """Mark a repo status=skip with a required reason. Never removes the entry.

    NOTE: inert today -- see the module docstring. This only records intent.
    """
    if not reason or not reason.strip():
        raise ValueError("skip requires a non-empty reason")
    registry = load_registry()
    entry = find(registry, owner, repo)
    if not entry:
        raise ValueError(f"{owner}/{repo} not found in registry -- can't skip a repo that isn't tracked")
    entry["status"] = "skip"
    entry["skip_reason"] = reason.strip()
    entry["skip_date"] = datetime.date.today().isoformat()
    save_registry(registry)
    return entry


def unskip(owner: str, repo: str) -> dict:
    registry = load_registry()
    entry = find(registry, owner, repo)
    if not entry:
        raise ValueError(f"{owner}/{repo} not found in registry")
    entry["status"] = "active"
    entry.pop("skip_reason", None)
    entry.pop("skip_date", None)
    save_registry(registry)
    return entry


def load_repo_seeds() -> list[dict]:
    if not REPO_SEEDS_FILE.exists():
        return []
    return json.loads(REPO_SEEDS_FILE.read_text())


def save_repo_seeds(repo_seeds: list[dict]) -> None:
    REPO_SEEDS_FILE.write_text(json.dumps(repo_seeds, indent=2) + "\n")


def mark_seed_pulled(name: str) -> dict:
    """Stamp repo_seeds.json's `last_pulled` = now for one seed list, after
    you've manually refreshed its vendored copy under repo-seeds/ (e.g.
    re-downloaded VoltAgent/awesome-agent-skills's README.md). Does not do
    the pulling itself -- there's no automated fetcher for these upstream
    lists today, only for the copy-on-disk's freshness bookkeeping. Run
    `./registry.py sync-seed` afterward to actually pick up any new repos
    the refreshed list contains."""
    repo_seeds = load_repo_seeds()
    entry = next((s for s in repo_seeds if s["name"] == name), None)
    if entry is None:
        raise ValueError(f"{name!r} not found in {REPO_SEEDS_FILE} -- known seeds: {[s['name'] for s in repo_seeds]}")
    entry["last_pulled"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    save_repo_seeds(repo_seeds)
    return entry


def sync_seeds() -> tuple[list[dict], list[dict]]:
    """Pick up every repo currently in each of SEED_FILES. Never destructive:
    a brand-new repo gets a new registry row, and a repo that's already
    tracked (regardless of what source found it first) just gets that seed
    list's own descriptor (`type` == the seed's `name`, e.g.
    "officialskills.sh") added to its existing `sources` list if it doesn't
    already have one -- surfacing the overlap instead of hiding it. Existing
    descriptors of *other* types (skip status, manual notes, other seed
    lists, etc.) are never touched. Runs every list in SEED_FILES in one
    pass and returns the combined (new_repos, newly_overlapping_repos).
    """
    import re

    url_re = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")

    registry = load_registry()
    new_repos, new_overlaps = [], []
    for seed in SEED_FILES:
        seen_this_pass = set()
        for owner, repo in url_re.findall(seed["path"].read_text()):
            repo = repo.rstrip(".")
            key = (owner.lower(), repo.lower())
            if key in seen_this_pass:
                continue
            seen_this_pass.add(key)

            existing = find(registry, owner, repo)
            had_this_seed_source = existing is not None and seed["name"] in source_types(existing)
            entry = upsert(registry, {
                "owner": owner,
                "repo": repo,
                "source": seed["name"],
                "file": seed["file"],
            })
            if existing is None:
                new_repos.append(entry)
            elif not had_this_seed_source:
                new_overlaps.append(entry)

    save_registry(registry)
    return new_repos, new_overlaps


def set_stars(owner: str, repo: str, stars: int) -> dict:
    """Record the GitHub stargazer count fetched by clone_repos.py, along
    with when it was fetched -- so consumers (index_qdrant.py, CSV export)
    know both the value and its freshness. No-ops silently if the repo
    isn't in the registry (e.g. a one-off URL clone never added via
    add-manual/add-search)."""
    registry = load_registry()
    entry = find(registry, owner, repo)
    if not entry:
        return {}
    entry["stars"] = stars
    entry["stars_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_registry(registry)
    return entry


def mark_sync_failure(owner: str, repo: str, reason: str) -> dict:
    """Record that a clone/sync attempt failed, without touching last_synced
    (which only ever reflects the last *successful* clone+RAG pass). Called
    by clone_repos.py when a clone errors, and by mark_synced_from_disk as a
    fallback for any repo still missing a directory for an unknown reason.
    """
    registry = load_registry()
    entry = find(registry, owner, repo)
    if not entry:
        raise ValueError(f"{owner}/{repo} not found in registry")
    entry["last_sync_failure"] = datetime.datetime.now().isoformat(timespec="seconds")
    entry["last_sync_failure_reason"] = reason.strip()
    save_registry(registry)
    return entry


def mark_synced_pairs(pairs: list[tuple[str, str]]) -> list[dict]:
    """Stamp last_synced=now on exactly the given (owner, repo) entries,
    without checking repos/ for a directory. Used by batch_pipeline.py,
    where repos/ only ever holds the *current* batch's clones (earlier
    batches' repos/ dirs are deleted to bound disk usage), so
    mark_synced_from_disk's "no directory -> mark failure" logic would
    wrongly flag every already-processed repo from a prior batch.

    Callers are responsible for only passing pairs that are actually
    confirmed on disk at some point (e.g. present in .clone_state.json) --
    this function does no verification itself.
    """
    registry = load_registry()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    pair_set = set(pairs)
    synced = []
    for entry in registry:
        if (entry["owner"], entry["repo"]) in pair_set:
            entry["last_synced"] = now
            entry.pop("last_sync_failure", None)
            entry.pop("last_sync_failure_reason", None)
            synced.append(entry)
    save_registry(registry)
    return synced


def mark_synced_from_disk() -> tuple[list[dict], list[dict]]:
    """Stamp last_synced=now on every registry entry that has a directory
    under repos/ -- meaning it's been cloned AND (since this is only called
    after extract_search_raw.py + index_qdrant.py both succeed) run through
    RAG. Returns (synced, missing) where missing = entries with no repos/
    directory at all (e.g. clone failed -- never made it to disk).

    A successful sync clears any prior last_sync_failure -- the repo is
    fine now, whatever failed before no longer applies.
    """
    registry = load_registry()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    synced, missing = [], []
    for entry in registry:
        if (REPOS_DIR / entry["owner"] / entry["repo"]).is_dir():
            entry["last_synced"] = now
            entry.pop("last_sync_failure", None)
            entry.pop("last_sync_failure_reason", None)
            synced.append(entry)
        else:
            if "last_sync_failure" not in entry:
                entry["last_sync_failure"] = now
                entry["last_sync_failure_reason"] = "no directory found under repos/ after clone_repos.py ran"
            missing.append(entry)
    save_registry(registry)
    return synced, missing


# How long a repo that just failed to clone (404, private, rename, etc.)
# sits out before unsynced_today() offers it up again -- mirrors
# clone_repos.py's RECLONE_COOLDOWN_SECONDS (30 days) for successful clones,
# so a dead repo doesn't get retried on every single batch forever (it used
# to: mark_sync_failure stamped a timestamp but nothing ever read it).
FAILURE_RETRY_COOLDOWN_DAYS = 7


def unsynced_today(registry: list[dict] | None = None) -> list[dict]:
    """Active entries whose last_synced isn't from today (or is missing
    entirely -- never made it through a full clone+RAG pipeline run).

    Entries with a recent last_sync_failure (within
    FAILURE_RETRY_COOLDOWN_DAYS) are excluded -- they'll naturally resurface
    once the cooldown expires, same as a repo that's due for its next
    30-day reclone. This keeps a persistently-broken repo (deleted/renamed/
    private upstream) from being re-selected on every batch forever."""
    registry = registry if registry is not None else load_registry()
    today = datetime.date.today().isoformat()
    cutoff = datetime.datetime.now() - datetime.timedelta(days=FAILURE_RETRY_COOLDOWN_DAYS)
    result = []
    for r in registry:
        if r.get("status", "active") != "active":
            continue
        last_synced = r.get("last_synced")
        if last_synced and last_synced.startswith(today):
            continue
        last_failure = r.get("last_sync_failure")
        if last_failure and datetime.datetime.fromisoformat(last_failure) > cutoff:
            continue
        result.append(r)
    return result


def remove(owner: str, repo: str) -> bool:
    registry = load_registry()
    entry = find(registry, owner, repo)
    if not entry:
        return False
    registry.remove(entry)
    save_registry(registry)
    return True


def _describe_source(descriptor: dict) -> str:
    t = descriptor["type"]
    if t == "officialskills.sh":
        return f"officialskills.sh:{descriptor.get('file')}"
    if t == "search":
        return f"search:{descriptor.get('query')!r} ({descriptor.get('sort')})"
    if t == "manual":
        return f"manual:{descriptor.get('note')}"
    if t == "marketplace":
        return f"marketplace:{descriptor.get('plugin')}"
    return t


def repo_pairs(registry: list[dict] | None = None, source: str | None = None) -> list[tuple[str, str]]:
    """(owner, repo) pairs for clone_repos.py, in registry order.

    Does NOT filter out status=="skip" entries yet -- skip is currently
    informational only (see module docstring). When that's wired up, this
    is the function that should start excluding them.

    `source`, if given, restricts to entries that have a descriptor of that
    type in `sources` (e.g. source="skills.sh" to clone only repos surfaced
    by the leaderboard, regardless of what other channels have also found
    them since).
    """
    registry = registry if registry is not None else load_registry()
    if source is not None:
        if source not in VALID_SOURCES:
            raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")
        registry = [r for r in registry if source in source_types(r)]
    return [(r["owner"], r["repo"]) for r in registry]


def _cli():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_manual = sub.add_parser("add-manual", help="Add one repo by hand with a required reason")
    p_manual.add_argument("owner_repo", help="owner/repo")
    p_manual.add_argument("note", help="Why this repo was added")

    p_search = sub.add_parser("add-search", help="Copy approved repos from a search_github.py JSON file")
    p_search.add_argument("results_json", help="Path to search_github.py --format json output")
    p_search.add_argument("--approve", action="append", required=True, dest="approved",
                           help="owner/repo to approve (repeatable)")

    sub.add_parser("sync-seed", help="Additively pick up new repos from every list in SEED_FILES")

    sub.add_parser("seeds", help="List every vendored seed list and when its on-disk copy was last pulled (repo_seeds.json)")

    p_seed_pulled = sub.add_parser("mark-seed-pulled", help="Stamp repo_seeds.json last_pulled=now for one seed list, after manually refreshing its vendored copy")
    p_seed_pulled.add_argument("name", help="Seed name, e.g. officialskills.sh")

    p_skip = sub.add_parser("skip", help="Mark a repo status=skip with a required reason (inert today, see docstring)")
    p_skip.add_argument("owner_repo", help="owner/repo")
    p_skip.add_argument("reason", help="Why this repo is being skipped")

    p_unskip = sub.add_parser("unskip", help="Reset a repo back to status=active")
    p_unskip.add_argument("owner_repo", help="owner/repo")

    sub.add_parser("mark-synced", help="Stamp last_synced=now on every repo present in repos/ (run by archived/run_pipeline.sh)")
    sub.add_parser("unsynced", help="List active repos not synced today (never synced, or stale)")

    p_list = sub.add_parser("list", help="List registry entries")
    p_list.add_argument("--source", choices=sorted(VALID_SOURCES))
    p_list.add_argument("--status", choices=["active", "skip"])

    p_remove = sub.add_parser("remove", help="Hard-delete a repo from the registry (mistakes only -- use skip for quality judgments)")
    p_remove.add_argument("owner_repo", help="owner/repo")

    p_skillsh = sub.add_parser("update-skillsh", help="Refresh skills.sh rank/skill_count/top_installs for one repo, independent of clone/index state")
    p_skillsh.add_argument("owner_repo", help="owner/repo")
    p_skillsh.add_argument("--rank", type=int)
    p_skillsh.add_argument("--skill-count", type=int)
    p_skillsh.add_argument("--top-installs", type=int)

    args = parser.parse_args()

    if args.cmd == "add-manual":
        owner, _, repo = args.owner_repo.partition("/")
        entry = add_manual(owner, repo, args.note)
        print(f"Added {entry['owner']}/{entry['repo']} (source=manual)")

    elif args.cmd == "add-search":
        added = add_search_results(Path(args.results_json), args.approved)
        for entry in added:
            search_descriptor = next(s for s in entry["sources"] if s["type"] == "search")
            print(f"Added {entry['owner']}/{entry['repo']} (source=search, query={search_descriptor['query']!r})")

    elif args.cmd == "sync-seed":
        new_repos, new_overlaps = sync_seeds()
        for entry in new_repos:
            print(f"Added {entry['owner']}/{entry['repo']} (source={'+'.join(source_types(entry))})")
        for entry in new_overlaps:
            print(f"{entry['owner']}/{entry['repo']} already tracked -- also found via a seed list (now {'+'.join(source_types(entry))})")
        print(f"{len(new_repos)} new repo(s), {len(new_overlaps)} newly-overlapping repo(s)", file=sys.stderr)

    elif args.cmd == "seeds":
        repo_seeds = load_repo_seeds()
        known = {s["name"] for s in SEED_FILES}
        tracked = {s["name"] for s in repo_seeds}
        for s in repo_seeds:
            print(f"{s['name']:<20} last_pulled={s.get('last_pulled', 'never')}  upstream={s.get('upstream_repo')}")
        missing = known - tracked
        if missing:
            print(f"[warn] in SEED_FILES but missing from {REPO_SEEDS_FILE}: {sorted(missing)}", file=sys.stderr)

    elif args.cmd == "mark-seed-pulled":
        entry = mark_seed_pulled(args.name)
        print(f"Marked {entry['name']} last_pulled={entry['last_pulled']}")

    elif args.cmd == "skip":
        owner, _, repo = args.owner_repo.partition("/")
        entry = skip(owner, repo, args.reason)
        print(f"Marked {entry['owner']}/{entry['repo']} status=skip (note: inert today, see registry.py docstring)")

    elif args.cmd == "unskip":
        owner, _, repo = args.owner_repo.partition("/")
        entry = unskip(owner, repo)
        print(f"Reset {entry['owner']}/{entry['repo']} to status=active")

    elif args.cmd == "mark-synced":
        synced, missing = mark_synced_from_disk()
        print(f"Marked {len(synced)} repo(s) synced")
        if missing:
            print(f"[warn] {len(missing)} repo(s) have no directory under repos/ (never cloned successfully):", file=sys.stderr)
            for entry in missing:
                print(f"  {entry['owner']}/{entry['repo']}", file=sys.stderr)

    elif args.cmd == "unsynced":
        registry = load_registry()
        stale = unsynced_today(registry)
        for r in stale:
            last = r.get("last_synced", "never")
            print(f"{r['owner']}/{r['repo']:<30} last_synced={last}")
        print(f"\n{len(stale)} of {len(registry)} active repos not synced today", file=sys.stderr)

    elif args.cmd == "list":
        registry = load_registry()
        if args.source:
            registry = [r for r in registry if args.source in source_types(r)]
        if args.status:
            registry = [r for r in registry if r.get("status", "active") == args.status]
        for r in registry:
            types = "+".join(s["type"] for s in r["sources"])
            details = "; ".join(_describe_source(s) for s in r["sources"])
            status = r.get("status", "active")
            tag = f"skip: {r.get('skip_reason')}" if status == "skip" else status
            print(f"{r['owner']}/{r['repo']:<30} [{types}] [{tag}] {details}")
        print(f"\n{len(registry)} repos", file=sys.stderr)

    elif args.cmd == "remove":
        owner, _, repo = args.owner_repo.partition("/")
        if remove(owner, repo):
            print(f"Removed {owner}/{repo}")
        else:
            print(f"[error] {owner}/{repo} not found in registry", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "update-skillsh":
        owner, _, repo = args.owner_repo.partition("/")
        try:
            entry = update_skillsh(owner, repo, rank=args.rank, skill_count=args.skill_count,
                                    top_installs=args.top_installs)
        except ValueError as e:
            print(f"[error] {e}", file=sys.stderr)
            sys.exit(1)
        descriptor = next(s for s in entry["sources"] if s["type"] == "skills.sh")
        print(f"Updated {entry['owner']}/{entry['repo']} skills.sh: rank={descriptor.get('rank')} "
              f"skill_count={descriptor.get('skill_count')} top_installs={descriptor.get('top_installs')}")


if __name__ == "__main__":
    _cli()
