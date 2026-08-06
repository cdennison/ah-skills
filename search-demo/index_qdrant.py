#!/usr/bin/env python3
"""Index SKILL.md files from /search-raw into a local Qdrant collection.

Uses Qdrant's built-in FastEmbed integration (models.Document) so embedding
happens automatically on upload/query -- no separate embedding step needed.

Incremental: each point's id is a hash of its *whitespace-normalized
content* (not its path), so byte-identical-modulo-formatting SKILL.md files
-- the same skill vendored at two paths in one repo, or copy-pasted across
repos with a stray newline/space difference in the frontmatter -- collapse
into a single point instead of one duplicate hit per copy. Its payload
carries the content hash plus a `locations` list recording every (owner,
repo, path) it was found at, so provenance for every copy is preserved even
though only one gets embedded. Re-running only (re-)embeds content that is
new or changed, and removes points whose content disappeared entirely. A
from-scratch run (empty/missing collection) costs the same as before.

Points whose `name` collides with another point's `name` (same skill name,
different content -- either a genuinely different skill that happens to
share a generic name like "setup" or "auth", or a lightly-reworded fork of a
vendored template like "skill-creator") are NOT merged -- content is the
only thing that's ever treated as "the same skill". Instead each payload
carries `name_collision_count` and `name_shared_with` so this is visible in
search results and the CSV export without silently conflating unrelated
skills.

Run with `--metadata-only` to skip content extraction/embedding entirely and
just re-derive stars/sources/ranking payload fields from registry.json for
every point already in the collection (see refresh_metadata()). Use this
after updating a repo's registry ranking data (e.g. `registry.py
update-skillsh`, or a fresh add_skillsh_leaderboard.py run) -- it's a cheap
payload-only push that works even if the repo's clone under repos/ is gone.

Default "is this already indexed" check is by filename (owner/repo/path
already present in some point's `locations`) -- a metadata-only scroll, no
disk reads for already-known files. This is a lot faster than the old
default of reading+hashing every file in search-raw/ on every run just to
find the (usually tiny) new/changed set, at the cost of not catching a file
whose *content* changed at a path that was already indexed. Pass `--hash`
to fall back to the full content-hash diff when you need that.

Embedding uploads happen in chunks of `--batch-size` (default 10000) so
progress is visible on large runs instead of one silent multi-hour call.
"""

import argparse
import hashlib
import re
import uuid
from collections import defaultdict
from pathlib import Path

from qdrant_client import QdrantClient, models
from tqdm import tqdm

import registry
from frontmatter import parse_frontmatter

SEARCH_RAW_DIR = Path(__file__).parent / "search-raw"
DB_PATH = Path(__file__).parent / "qdrant_db"
COLLECTION = "agent_skills"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL_NAME = "Qdrant/bm25"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Qdrant point ids must be an unsigned int or a UUID -- an arbitrary hex
# digest is rejected, so derive a stable UUID5 from the content hash instead
# (identical content always maps to the same id across runs, regardless of
# which path(s) it lives at).
POINT_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def point_id(content_hash_hex: str) -> str:
    return str(uuid.uuid5(POINT_ID_NAMESPACE, content_hash_hex))


def content_hash(text: str) -> str:
    # Collapse whitespace runs before hashing so two copies of the same
    # skill that differ only by a stray newline/space (e.g. one frontmatter
    # description word-wrapped, the other not) still dedupe. The original
    # `text` -- not this normalized form -- is what actually gets embedded
    # and stored, so formatting is preserved for display/content purposes.
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


# registry.json source-descriptor keys that are bookkeeping/provenance, not
# a ranking/popularity stat -- everything else numeric is surfaced. Generic
# on purpose: it's an N (source types) x N (stat fields per type) matrix --
# skills.sh has rank/top_installs/skill_count today, search has rank, a
# future source (e.g. npm downloads, a different leaderboard) can add its
# own numeric fields and they show up here with zero code changes.
_RANKING_EXCLUDE_KEYS = {
    "type", "added", "query", "sort", "exact", "reviewed_date", "note", "plugin", "seed_file",
    "rank_last_updated",
}


def _ranking_string(registry_entry: dict | None) -> str:
    """Flatten every ranking/popularity metric found across ALL of a repo's
    registry.json source descriptors into one space-separated `key=value`
    string, namespaced `{source_type}_{field}=value` (e.g. `skills_sh_rank=3193
    skills_sh_top_installs=3019 search_rank=12`). A repo can carry multiple source
    descriptors at once and each can carry multiple stat fields, so this is
    one column covering the full N-sources x N-stats matrix rather than a
    fixed set of payload fields tied to specific source types."""
    if not registry_entry:
        return ""
    tokens = []
    for s in registry_entry.get("sources", []):
        source_key = s["type"].replace(".", "_").replace("-", "_")
        for field, value in s.items():
            if field in _RANKING_EXCLUDE_KEYS or value is None:
                continue
            if not isinstance(value, (int, float)):
                continue
            tokens.append(f"{source_key}_{field}={value}")
    return " ".join(sorted(tokens))


def _primary_location(locations: list[dict]) -> dict:
    """Pick one location to flatten onto the payload's top-level owner/repo/
    path/etc columns (for the CSV export and any consumer that just wants
    "a" repo, not all of them) -- most-starred first, then alphabetical for
    a stable tie-break."""
    return max(locations, key=lambda loc: (loc["stars"] or 0, loc["owner"], loc["repo"], loc["path"]))


def load_skills(skip_paths: set[str] | None = None):
    """skip_paths: relative-path strings (as produced by str(rel) below) to
    skip entirely -- no read, no hash. Used by the fast filename-based mode
    to avoid touching files already known to be indexed."""
    skip_paths = skip_paths or set()
    registry_by_repo = {
        (r["owner"].lower(), r["repo"].lower()): r for r in registry.load_registry()
    }

    by_hash: dict[str, dict] = {}
    for path in sorted(SEARCH_RAW_DIR.rglob("*.md")):
        if not path.is_file():
            print(f"[warn] skipping non-file {path.relative_to(SEARCH_RAW_DIR)} (directory named like a .md file)")
            continue
        rel = path.relative_to(SEARCH_RAW_DIR)
        if str(rel) in skip_paths:
            continue
        owner, repo = rel.parts[0], rel.parts[1]
        subpath = "/".join(rel.parts[2:])
        text = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        registry_entry = registry_by_repo.get((owner.lower(), repo.lower()))
        # sources: every registry.json discovery channel (seed/search/manual/
        # marketplace) that surfaced this repo -- see registry.py. Re-derived
        # from registry.json on every index run, so a repo that gains a new
        # source between runs picks it up on the next rebuild.
        sources = sorted(registry.source_types(registry_entry)) if registry_entry else []
        stars = registry_entry.get("stars") if registry_entry else None
        ranking = _ranking_string(registry_entry)
        h = content_hash(text)

        group = by_hash.setdefault(
            h,
            {
                "content_hash": h,
                "name": meta.get("name", path.parent.name),
                "description": meta.get("description", ""),
                "content": text,
                "locations": [],
            },
        )
        group["locations"].append(
            {
                "owner": owner,
                "repo": repo,
                "path": str(rel),
                "repo_url": f"https://github.com/{owner}/{repo}",
                # blob/HEAD resolves to whatever the default branch currently
                # is, so this stays valid even if a repo renames main/master.
                "skill_url": f"https://github.com/{owner}/{repo}/blob/HEAD/{subpath}",
                "sources": sources,
                "stars": stars,
                "ranking": ranking,
            }
        )

    points = []
    for h, group in by_hash.items():
        locations = group["locations"]
        primary = _primary_location(locations)
        all_sources = sorted({s for loc in locations for s in loc["sources"]})
        points.append(
            {
                "id": point_id(h),
                "content_hash": h,
                "name": group["name"],
                "description": group["description"],
                "content": group["content"],
                # flattened for consumers that just want "a" repo (CSV
                # export, older payload readers) -- the full set of copies
                # lives in `locations` and `duplicate_count`.
                "owner": primary["owner"],
                "repo": primary["repo"],
                "path": primary["path"],
                "repo_url": primary["repo_url"],
                "skill_url": primary["skill_url"],
                "stars": primary["stars"],
                "ranking": primary["ranking"],
                "sources": all_sources,
                "locations": locations,
                "duplicate_count": len(locations),
            }
        )

    # Same skill *name* used by distinct content -- flagged, never merged
    # (see module docstring for why: could be a coincidental generic name
    # or a reworded fork, and only content identity is a safe merge key).
    points_by_name = defaultdict(list)
    for p in points:
        points_by_name[p["name"].strip().lower()].append(p)

    for p in points:
        others = [q for q in points_by_name[p["name"].strip().lower()] if q is not p]
        p["name_collision_count"] = len(others)
        p["name_shared_with"] = sorted({f"{q['owner']}/{q['repo']}" for q in others})

    yield from points


def existing_hashes(client: QdrantClient) -> dict:
    """Map of point id -> content_hash currently stored in the collection."""
    hashes = {}
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION,
            with_payload=["content_hash"],
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        for p in points:
            hashes[p.id] = p.payload.get("content_hash")
        if offset is None:
            break
    return hashes


def refresh_metadata(client: QdrantClient) -> int:
    """Re-derive stars/sources/ranking from registry.json for every point
    already in the collection and push a payload-only update -- no
    re-embedding, no dependency on search-raw/ or repos/ having the repo's
    files on disk. This is what makes "I just need to update this repo's
    skills.sh rank/install count" cheap: `registry.py update-skillsh` (or a
    fresh `add_skillsh_leaderboard.py` run) only touches registry.json, and
    this function is what actually gets that change into Qdrant/search
    results/CSV exports, even for a repo whose repos/<owner>/<repo> clone
    was deleted after indexing.
    """
    registry_by_repo = {
        (r["owner"].lower(), r["repo"].lower()): r for r in registry.load_registry()
    }

    updated = 0
    offset = None
    while True:
        points, offset = client.scroll(COLLECTION, with_payload=True, with_vectors=False, limit=1000, offset=offset)
        for p in points:
            payload = p.payload or {}
            locations = payload.get("locations") or []
            changed = False
            new_locations = []
            for loc in locations:
                entry = registry_by_repo.get((loc["owner"].lower(), loc["repo"].lower()))
                new_sources = sorted(registry.source_types(entry)) if entry else []
                new_stars = entry.get("stars") if entry else None
                new_ranking = _ranking_string(entry)
                if new_sources != loc.get("sources") or new_stars != loc.get("stars") or new_ranking != loc.get("ranking"):
                    changed = True
                loc = {**loc, "sources": new_sources, "stars": new_stars, "ranking": new_ranking}
                new_locations.append(loc)
            if not new_locations:
                continue
            primary = _primary_location(new_locations)
            all_sources = sorted({s for loc in new_locations for s in loc["sources"]})
            new_payload = {
                "locations": new_locations,
                "stars": primary["stars"],
                "ranking": primary["ranking"],
                "sources": all_sources,
            }
            if changed or new_payload["sources"] != payload.get("sources") or new_payload["stars"] != payload.get("stars") \
                    or new_payload["ranking"] != payload.get("ranking"):
                client.set_payload(COLLECTION, payload=new_payload, points=[p.id])
                updated += 1
        if offset is None:
            break
    return updated


def known_paths(client) -> set[str]:
    """Every location path already recorded in the collection (owner/repo/path
    strings, matching str(rel) in load_skills). Metadata-only scroll -- no
    vectors, no disk reads -- so this is cheap even at 100k+ points."""
    paths = set()
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, with_payload=["locations"], with_vectors=False, limit=1000, offset=offset
        )
        for p in points:
            for loc in (p.payload or {}).get("locations") or []:
                paths.add(loc["path"])
        if offset is None:
            break
    return paths


def current_paths() -> set[str]:
    """Every skill file path currently under search-raw/, no content read."""
    paths = set()
    for path in SEARCH_RAW_DIR.rglob("*.md"):
        if not path.is_file():
            continue
        paths.add(str(path.relative_to(SEARCH_RAW_DIR)))
    return paths


def prune_stale_locations(client, current: set[str]) -> tuple[int, int]:
    """Drop locations whose path no longer exists under search-raw/; delete
    any point left with zero locations. Returns (points_deleted, points_updated)."""
    deleted = 0
    updated = 0
    to_delete_ids = []
    offset = None
    while True:
        points, offset = client.scroll(COLLECTION, with_payload=True, with_vectors=False, limit=1000, offset=offset)
        for p in points:
            payload = p.payload or {}
            locations = payload.get("locations") or []
            kept = [loc for loc in locations if loc["path"] in current]
            if len(kept) == len(locations):
                continue
            if not kept:
                to_delete_ids.append(p.id)
                deleted += 1
                continue
            primary = _primary_location(kept)
            all_sources = sorted({s for loc in kept for s in loc["sources"]})
            new_payload = {
                "locations": kept,
                "owner": primary["owner"],
                "repo": primary["repo"],
                "path": primary["path"],
                "repo_url": primary["repo_url"],
                "skill_url": primary["skill_url"],
                "stars": primary["stars"],
                "ranking": primary["ranking"],
                "sources": all_sources,
                "duplicate_count": len(kept),
            }
            client.set_payload(COLLECTION, payload=new_payload, points=[p.id])
            updated += 1
        if offset is None:
            break
    if to_delete_ids:
        client.delete(COLLECTION, points_selector=models.PointIdsList(points=to_delete_ids))
    return deleted, updated


def upload_in_batches(client, skills: list[dict], batch_size: int) -> None:
    total = len(skills)
    with tqdm(total=total, unit="skill", desc="embedding", smoothing=0.1) as bar:
        for i in range(0, total, batch_size):
            chunk = skills[i : i + batch_size]
            vectors = [
                {
                    DENSE_VECTOR_NAME: models.Document(
                        text=f"{s['name']}: {s['description']}\n\n{s['content']}", model=MODEL_NAME
                    ),
                    SPARSE_VECTOR_NAME: models.Document(
                        text=f"{s['name']}: {s['description']}\n\n{s['content']}", model=SPARSE_MODEL_NAME
                    ),
                }
                for s in chunk
            ]
            client.upload_collection(
                collection_name=COLLECTION,
                vectors=vectors,
                payload=chunk,
                ids=[s["id"] for s in chunk],
            )
            bar.update(len(chunk))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-only", action="store_true",
        help="Skip content extraction/embedding; only refresh stars/sources/ranking on existing points.",
    )
    parser.add_argument(
        "--hash", action="store_true",
        help="Diff by content hash (reads+hashes every file in search-raw/ every run) instead of the "
        "default fast filename-based check. Slower, but also catches a file whose content changed "
        "at a path that was already indexed.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10_000,
        help="Skills per upload_collection call, so progress is visible on large runs (default 10000).",
    )
    args = parser.parse_args()

    client = QdrantClient(path=str(DB_PATH))

    if args.metadata_only:
        if not client.collection_exists(COLLECTION):
            raise SystemExit(f"Collection {COLLECTION!r} not found at {DB_PATH} -- run a full index first")
        updated = refresh_metadata(client)
        print(f"Refreshed stars/sources/ranking on {updated} point(s) from registry.json (no re-embedding, no disk read)")
        return

    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=client.get_embedding_size(MODEL_NAME),
                    distance=models.Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(
                    modifier=models.Modifier.IDF,
                ),
            },
        )

    if args.hash:
        skills = list(load_skills())
        current_ids = {s["id"] for s in skills}
        old_hashes = existing_hashes(client)

        changed = [s for s in skills if old_hashes.get(s["id"]) != s["content_hash"]]
        stale_ids = [pid for pid in old_hashes if pid not in current_ids]

        if stale_ids:
            client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale_ids))
        if changed:
            upload_in_batches(client, changed, args.batch_size)

        print(
            f"Indexed {len(skills)} skill files into {DB_PATH} (collection={COLLECTION!r}): "
            f"{len(changed)} new/changed, {len(stale_ids)} removed, "
            f"{len(skills) - len(changed)} unchanged"
        )
    else:
        current = current_paths()
        known = known_paths(client)
        new_skills = list(load_skills(skip_paths=known))
        deleted, updated = prune_stale_locations(client, current)

        if new_skills:
            upload_in_batches(client, new_skills, args.batch_size)

        print(
            f"Indexed {len(new_skills)} new skill file(s) by filename check "
            f"(skipped {len(known)} already-known path(s)); pruned {deleted} removed point(s), "
            f"updated {updated} point(s) with partial removals. "
            f"Use --hash for a full content-hash re-diff (catches edited files, slower)."
        )


if __name__ == "__main__":
    main()
