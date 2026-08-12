#!/usr/bin/env python3
"""Index SKILL.md files from /search-raw into a local Qdrant collection.

Embeds locally via fastembed (the same dense MiniLM + sparse BM25 models
Qdrant's built-in FastEmbed integration would use) with explicit control
over onnxruntime thread count and memory arena -- see the memory note
below for why that control matters and upload_in_batches() for how
embedding is done.

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
`--batch-size` only controls how many points get grouped per progress-bar
tick / `upsert` call though -- the actual embedding inference batch size
(how many documents are tokenized/run through onnxruntime at once) is
controlled separately by `--embed-batch-size`, see upload_in_batches().

Memory note: embedding used to go through qdrant_client's automatic
`models.Document` inference (client.upload_collection), which loads the
dense (MiniLM) and sparse (BM25) onnxruntime sessions with
intra_op_num_threads left at onnxruntime's default -- one thread pool *per
CPU core* for *each* of the two models, each with its own growing
CPU-arena allocator that never shrinks back. On an N-core box that's up to
2N thread-local arenas alive for the life of the process, which is what
was driving anon-RSS to ~2.3GB for just 1000 short documents on a 3.7GB
box -- not the documents themselves (a few hundred KB of text) or the
per-chunk payload lists. qdrant_client's Document-inference path doesn't
expose onnxruntime's thread/arena knobs at all, so this script now
constructs the two fastembed models itself (get_embedder()) with an
explicit `--embed-threads` thread count and `enable_cpu_mem_arena=False`
(bounded per-call allocation instead of an ever-growing arena), and embeds
in small `--embed-batch-size` chunks so only one inference batch's worth
of token/output arrays is live at a time.
"""

import argparse
import hashlib
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path

from fastembed import SparseTextEmbedding, TextEmbedding
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

# SKILLS_QDRANT_URL lets a caller point at an alternate Qdrant server --
# e.g. a local embedded store via SKILLS_QDRANT_DB_PATH-style override --
# without touching code. Unset (the default) uses the Docker instance at
# localhost:6333.
QDRANT_URL = os.environ.get("SKILLS_QDRANT_URL", "http://localhost:6333")

# Defaults chosen for a small (~4GB, no-swap) box: 2 onnxruntime threads per
# model rather than one-per-core, and a small inference batch so only a
# handful of documents' tokenized/output arrays are ever live at once.
# Override with --embed-threads/--embed-batch-size (or the env vars, so
# RUN.sh/batch_pipeline.py callers don't need new flags) for bigger boxes.
DEFAULT_EMBED_THREADS = int(os.environ.get("SKILLS_EMBED_THREADS", "2"))
DEFAULT_EMBED_BATCH_SIZE = int(os.environ.get("SKILLS_EMBED_BATCH_SIZE", "16"))

_embedder_cache: dict[str, TextEmbedding | SparseTextEmbedding] = {}


def get_embedder(model_name: str, sparse: bool, threads: int) -> TextEmbedding | SparseTextEmbedding:
    """Construct (once) and cache the dense/sparse fastembed model with an
    explicit thread count and the CPU memory arena disabled. Going through
    fastembed directly -- rather than qdrant_client's automatic
    `models.Document` inference -- is what makes these onnxruntime knobs
    reachable at all; see the module docstring."""
    cache_key = f"{model_name}:{threads}"
    if cache_key not in _embedder_cache:
        cls = SparseTextEmbedding if sparse else TextEmbedding
        _embedder_cache[cache_key] = cls(
            model_name=model_name, threads=threads, enable_cpu_mem_arena=False
        )
    return _embedder_cache[cache_key]


def get_client() -> QdrantClient:
    """Shared entry point for constructing the Qdrant client used by every
    script in this pipeline. Defaults to the Docker server at localhost:6333;
    set SKILLS_QDRANT_URL to point elsewhere, or SKILLS_QDRANT_DB_PATH (see
    app/search.py) to use an embedded on-disk store instead."""
    db_path_override = os.environ.get("SKILLS_QDRANT_DB_PATH")
    if db_path_override:
        return QdrantClient(path=db_path_override)
    return QdrantClient(url=QDRANT_URL)

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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _ranking_string(registry_entry: dict | None) -> str:
    """Flatten every ranking/popularity metric found across ALL of a repo's
    registry.json source descriptors into one space-separated `key=value`
    string, namespaced `{source_type}_{field}=value` (e.g. `skills_sh_rank=3193
    skills_sh_top_installs=3019 search_rank_agent_skills_stars=12`). A repo
    can carry multiple source descriptors at once and each can carry multiple
    stat fields, so this is one column covering the full N-sources x N-stats
    matrix rather than a fixed set of payload fields tied to specific source
    types.

    "search" sources are a special case: the same repo routinely turns up
    under several distinct (query, sort) searches (e.g. "agent skills" x
    stars/best-match, "claude skills" x stars/best-match, "codex skills" x
    stars/best-match -- 6 combos today), each with its own independent rank.
    Keying all of them as a single `search_rank` token would silently
    collide/overwrite -- namespace by query+sort instead so every combo gets
    its own token (`search_rank_<query_slug>_<sort_slug>=N`)."""
    if not registry_entry:
        return ""
    tokens = []
    for s in registry_entry.get("sources", []):
        source_key = s["type"].replace(".", "_").replace("-", "_")
        is_search = s["type"] == "search"
        for field, value in s.items():
            if field in _RANKING_EXCLUDE_KEYS or value is None:
                continue
            if not isinstance(value, (int, float)):
                continue
            if is_search and field == "rank":
                query_slug = _slug(s.get("query", ""))
                sort_slug = _slug(s.get("sort", ""))
                key = f"search_rank_{query_slug}_{sort_slug}"
            else:
                key = f"{source_key}_{field}"
            tokens.append(f"{key}={value}")
    return " ".join(sorted(tokens))


def _search_rank_fields(registry_entry: dict | None) -> dict[str, int]:
    """Same `search_rank_<query_slug>_<sort_slug>` namespacing as
    `_ranking_string`, but returned as a dict of real values instead of
    tokens in a flattened string -- these get merged onto the point's
    top-level payload so Qdrant can filter on them natively (FieldCondition/
    Range), applied as part of the ANN search itself instead of a client-side
    post-filter over an overfetched candidate pool."""
    if not registry_entry:
        return {}
    fields: dict[str, int] = {}
    for s in registry_entry.get("sources", []):
        if s["type"] != "search":
            continue
        rank = s.get("rank")
        if rank is None or not isinstance(rank, (int, float)):
            continue
        query_slug = _slug(s.get("query", ""))
        sort_slug = _slug(s.get("sort", ""))
        fields[f"search_rank_{query_slug}_{sort_slug}"] = int(rank)
    return fields


_SEARCH_RANK_TOKEN_RE = re.compile(r"(?:^|\s)(search_rank_\S+?)=(\d+)")


def _parse_search_rank_tokens(ranking: str) -> dict[str, int]:
    """Recover {field_name: value} from an already-flattened `ranking`
    string -- used where a fresh registry.json lookup isn't available (e.g.
    `prune_stale_locations`, which only has each location's previously
    stored `ranking`), so pruning doesn't require re-deriving from the
    registry. Mirrors app/search.py's `parse_search_rank` / export_csv.py's
    `_SEARCH_RANK_TOKEN_RE` -- keep the three in sync."""
    return {name: int(value) for name, value in _SEARCH_RANK_TOKEN_RE.findall(ranking or "")}


def _replace_search_rank_fields(
    client: QdrantClient, point_id: str, old_payload: dict, new_fields: dict[str, int]
) -> None:
    """`client.set_payload` merges into existing payload rather than
    replacing it, so a search_rank_* field that disappears (repo drops out of
    a search ranking) would otherwise linger as a stale, silently-wrong
    filter match -- explicitly delete keys that are no longer present before
    setting the current ones."""
    stale_keys = {k for k in old_payload if k.startswith("search_rank_")} - new_fields.keys()
    if stale_keys:
        client.delete_payload(COLLECTION, keys=list(stale_keys), points=[point_id])
    if new_fields:
        client.set_payload(COLLECTION, payload=new_fields, points=[point_id])


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
        primary_registry_entry = registry_by_repo.get((primary["owner"].lower(), primary["repo"].lower()))
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
                # search_rank_<query_slug>_<sort_slug> top-level fields for
                # native Qdrant filtering -- see _search_rank_fields().
                **_search_rank_fields(primary_registry_entry),
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
            primary_registry_entry = registry_by_repo.get((primary["owner"].lower(), primary["repo"].lower()))
            new_rank_fields = _search_rank_fields(primary_registry_entry)
            new_payload = {
                "locations": new_locations,
                "stars": primary["stars"],
                "ranking": primary["ranking"],
                "sources": all_sources,
                **new_rank_fields,
            }
            old_rank_fields = {k: v for k, v in payload.items() if k.startswith("search_rank_")}
            if changed or new_payload["sources"] != payload.get("sources") or new_payload["stars"] != payload.get("stars") \
                    or new_payload["ranking"] != payload.get("ranking") or new_rank_fields != old_rank_fields:
                _replace_search_rank_fields(client, p.id, payload, new_rank_fields)
                client.set_payload(
                    COLLECTION,
                    payload={k: v for k, v in new_payload.items() if not k.startswith("search_rank_")},
                    points=[p.id],
                )
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
            new_rank_fields = _parse_search_rank_tokens(primary["ranking"])
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
            _replace_search_rank_fields(client, p.id, payload, new_rank_fields)
            client.set_payload(COLLECTION, payload=new_payload, points=[p.id])
            updated += 1
        if offset is None:
            break
    if to_delete_ids:
        client.delete(COLLECTION, points_selector=models.PointIdsList(points=to_delete_ids))
    return deleted, updated


def upload_in_batches(
    client, skills: list[dict], batch_size: int,
    embed_batch_size: int = DEFAULT_EMBED_BATCH_SIZE, embed_threads: int = DEFAULT_EMBED_THREADS,
) -> None:
    """batch_size groups points per upsert()/progress-bar tick (network-call
    granularity); embed_batch_size groups documents per onnxruntime
    inference call within a chunk (memory-use granularity) -- see module
    docstring for why these are no longer the same knob."""
    dense_model = get_embedder(MODEL_NAME, sparse=False, threads=embed_threads)
    sparse_model = get_embedder(SPARSE_MODEL_NAME, sparse=True, threads=embed_threads)

    total = len(skills)
    with tqdm(total=total, unit="skill", desc="embedding", smoothing=0.1) as bar:
        for i in range(0, total, batch_size):
            chunk = skills[i : i + batch_size]
            texts = [f"{s['name']}: {s['description']}\n\n{s['content']}" for s in chunk]

            dense_vecs = list(dense_model.embed(texts, batch_size=embed_batch_size))
            sparse_vecs = list(sparse_model.embed(texts, batch_size=embed_batch_size))

            points = [
                models.PointStruct(
                    id=s["id"],
                    vector={
                        DENSE_VECTOR_NAME: dense_vecs[j].tolist(),
                        SPARSE_VECTOR_NAME: models.SparseVector(
                            indices=sparse_vecs[j].indices.tolist(),
                            values=sparse_vecs[j].values.tolist(),
                        ),
                    },
                    payload=s,
                )
                for j, s in enumerate(chunk)
            ]
            client.upsert(collection_name=COLLECTION, points=points)
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
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of skill points indexed (applied after sort/dedup), e.g. for a quick test run.",
    )
    parser.add_argument(
        "--embed-threads", type=int, default=DEFAULT_EMBED_THREADS,
        help="onnxruntime intra/inter-op threads per embedding model (default 2, or $SKILLS_EMBED_THREADS). "
        "Left at onnxruntime's default (one thread pool per CPU core, per model) this is what OOM-kills "
        "indexing on small boxes -- see module docstring.",
    )
    parser.add_argument(
        "--embed-batch-size", type=int, default=DEFAULT_EMBED_BATCH_SIZE,
        help="Documents embedded per onnxruntime inference call (default 16, or $SKILLS_EMBED_BATCH_SIZE). "
        "Raise on boxes with more RAM for faster throughput.",
    )
    args = parser.parse_args()

    client = get_client()

    if args.metadata_only:
        if not client.collection_exists(COLLECTION):
            raise SystemExit(f"Collection {COLLECTION!r} not found -- run a full index first")
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
        if args.limit is not None:
            skills = skills[: args.limit]
        current_ids = {s["id"] for s in skills}
        old_hashes = existing_hashes(client)

        changed = [s for s in skills if old_hashes.get(s["id"]) != s["content_hash"]]
        stale_ids = [pid for pid in old_hashes if pid not in current_ids]

        if stale_ids:
            client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale_ids))
        if changed:
            upload_in_batches(client, changed, args.batch_size, args.embed_batch_size, args.embed_threads)

        print(
            f"Indexed {len(skills)} skill files into collection={COLLECTION!r}: "
            f"{len(changed)} new/changed, {len(stale_ids)} removed, "
            f"{len(skills) - len(changed)} unchanged"
        )
    else:
        current = current_paths()
        known = known_paths(client)
        new_skills = list(load_skills(skip_paths=known))
        if args.limit is not None:
            new_skills = new_skills[: args.limit]
        deleted, updated = prune_stale_locations(client, current)

        if new_skills:
            upload_in_batches(client, new_skills, args.batch_size, args.embed_batch_size, args.embed_threads)

        print(
            f"Indexed {len(new_skills)} new skill file(s) by filename check "
            f"(skipped {len(known)} already-known path(s)); pruned {deleted} removed point(s), "
            f"updated {updated} point(s) with partial removals. "
            f"Use --hash for a full content-hash re-diff (catches edited files, slower)."
        )


if __name__ == "__main__":
    main()
