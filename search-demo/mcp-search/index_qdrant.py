#!/usr/bin/env python3
"""Index mcp-repo-seeds/registry.json + downloaded readmes into a Qdrant
collection, separate from the skills pipeline's `agent_skills` collection --
own name (`mcp_servers`), own env-var namespace (MCP_QDRANT_URL/
MCP_QDRANT_DB_PATH/MCP_EMBED_THREADS/MCP_EMBED_BATCH_SIZE), own storage when
running embedded. See PROPOSED_PIPELINE.md's "why separate storage isn't
just tidiness" for why these two pipelines never share a collection or an
env-var namespace, even though they share the *mechanical* indexing code in
../shared/qdrant.py (client construction, bounded-memory embedding models,
size-capped batch upsert).

Much simpler dedup story than the skills indexer: mcp_registry.py already
produces exactly one row per unique server (deduped across the official
registry/Glama/seed-list sources by GitHub repo, or a source-scoped id when
no repo resolves) -- so this is one Qdrant point per registry row, no
locations/duplicate_count/name-collision machinery needed.

Incremental: each row's point id is a stable UUID5 of its registry `id`
(e.g. "github:owner/repo"), and a content_hash over name+description+readme
text decides whether a row needs (re-)embedding. A row whose readme just
finished downloading, or whose classification changed, naturally gets
picked up on the next run; a row removed from registry.json (shouldn't
normally happen -- the registry is additive) gets pruned.

Payload fields reuse export_mcp_csv.py's `first_descriptor_value` for the
same source-descriptor flattening the CSV export uses (registry_type,
package_identifier, package_url, deployment, license, ...) rather than
re-deriving it -- see that module for the field list and its rationale.

Usage:
    python index_qdrant.py                  # incremental (default)
    python index_qdrant.py --hash            # full content-hash re-diff
    python index_qdrant.py --limit 100       # quick test run
    python index_qdrant.py --rankings-only   # payload-only stars/downloads sync, no re-embed

    # Wipe-and-rebuild, reviewed before going wide (see ensure_collection()/
    # select_ranked_sample() -- this is the reusable path for both, not a
    # one-off client.delete_collection() call):
    python index_qdrant.py --rebuild --sample-ranked 50   # empty collection, index 50 ranked rows
    #   ...review the 50 points (payload, ranking fields, embeddings)...
    python index_qdrant.py                                 # then index everything else, unflagged
"""

import argparse
import hashlib
import itertools
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdrant_client import models
from tqdm import tqdm

import mcp_registry
from export_mcp_csv import first_descriptor_value
from shared.qdrant import get_client as _shared_get_client, get_embedder, upsert_size_capped

COLLECTION = "mcp_servers"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL_NAME = "Qdrant/bm25"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Own env-var namespace, deliberately never reusing the skills pipeline's
# SKILLS_* names -- see module docstring.
DEFAULT_EMBED_THREADS = int(os.environ.get("MCP_EMBED_THREADS", "2"))
DEFAULT_EMBED_BATCH_SIZE = int(os.environ.get("MCP_EMBED_BATCH_SIZE", "16"))

POINT_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c9")  # distinct
# from index_qdrant.py's skills namespace (...430c8) -- deliberately one
# digit different so a colliding registry `id` string and a colliding
# skill content-hash could never map to the same point id even by
# coincidence, though the two are never in the same collection anyway.


def get_client():
    return _shared_get_client("MCP_QDRANT_URL", "MCP_QDRANT_DB_PATH")


def point_id(registry_id: str) -> str:
    return str(uuid.uuid5(POINT_ID_NAMESPACE, registry_id))


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# Glama exposes a coarse hosting hint as an attribute string. scan_mcp's
# derived `deployment` (from a real server.json packages[]/remotes[] split,
# merged by enrich_from_repo_scan.py) is more precise and wins whenever
# present -- this mapping only fills the gap for a Glama-only row that was
# never repo-scanned, so the payload's `deployment`/`has_remote` aren't just
# null while the hosting fact sits unused in `attributes`.
_GLAMA_HOSTING_DEPLOYMENT = {
    "hosting:hybrid": "hybrid",
    "hosting:remote-only": "remote",
    "hosting:local-only": "local",
    # "hosting:remote-capable" -> a remote endpoint exists but local install
    # is still the primary path; flips has_remote, not deployment.
}
_GLAMA_REMOTE_ATTRS = {"hosting:hybrid", "hosting:remote-only", "hosting:remote-capable"}


def _attributes(entry: dict) -> list:
    return first_descriptor_value(entry, "attributes") or []


def resolve_deployment(entry: dict) -> str | None:
    scanned = first_descriptor_value(entry, "deployment")
    if scanned:
        return scanned
    for attr in _attributes(entry):
        mapped = _GLAMA_HOSTING_DEPLOYMENT.get(attr)
        if mapped:
            return mapped
    return None


def resolve_has_remote(entry: dict) -> bool:
    if bool(first_descriptor_value(entry, "has_remote")):
        return True
    return any(attr in _GLAMA_REMOTE_ATTRS for attr in _attributes(entry))


def readme_text(entry: dict) -> str:
    readme_path = entry.get("readme_path")
    if not readme_path:
        return ""
    full_path = mcp_registry.MCP_DIR.parent / readme_path
    if not full_path.exists():
        return ""
    return full_path.read_text(errors="ignore")


def load_points(registry_rows: list[dict], skip_ids: set[str] | None = None):
    skip_ids = skip_ids or set()
    for entry in registry_rows:
        if entry["id"] in skip_ids:
            continue
        name = entry.get("name") or entry["id"]
        description = entry.get("description") or ""
        readme = readme_text(entry)
        h = content_hash(f"{name}\n{description}\n{readme}")

        # Glama alone isn't enough as a description source -- confirmed by
        # hand (test-data/openzim-mcp-cluster/DESCRIPTION_COMPARISON.md,
        # test_e2e_pipeline.py's TestDescriptionCapture): it sometimes
        # synthesizes real signal beyond the README, sometimes just echoes
        # the README's own tagline verbatim, adding nothing. Both signals
        # are captured here distinctly rather than trusting the single
        # merged `description` field above, which is genuinely lossy (only
        # ever one source's text, decided by mcp_registry.upsert()'s
        # priority rule, not "the best available description"). Neither is
        # part of content_hash -- glama_description already lives inside a
        # `sources[]` entry that changing would already re-embed via a
        # different path if it mattered there, and readme_description is
        # purely derived from `readme`, which IS already hashed above.
        glama_source = next((s for s in entry.get("sources", []) if s["type"] == "glama"), None)
        glama_description = glama_source.get("description") if glama_source else None
        readme_description = mcp_registry.extract_readme_description(readme)

        yield {
            "id": point_id(entry["id"]),
            "mcp_id": entry["id"],
            "content_hash": h,
            "name": name,
            "description": description,
            "glama_description": glama_description,
            "readme_description": readme_description,
            "readme": readme,
            "repo_url": entry.get("repo_url"),
            "status": entry.get("status"),
            "mcp_category": entry.get("mcp_category"),
            "mcp_category_source": entry.get("mcp_category_source"),
            # Real list (not export_mcp_csv.py's "+".join string) so Qdrant
            # can natively filter with MatchAny, same pattern as the skills
            # collection's `sources` field.
            "sources": [s["type"] for s in entry.get("sources", [])],
            "source_count": len(entry.get("sources", [])),
            "registry_type": first_descriptor_value(entry, "registry_type"),
            "package_identifier": first_descriptor_value(entry, "package_identifier"),
            "package_url": first_descriptor_value(entry, "package_url"),
            # scan_mcp-derived deployment/transport (enrich_from_repo_scan.py),
            # with a Glama `hosting:` attribute as the deployment/has_remote
            # fallback -- see resolve_deployment()/resolve_has_remote().
            "deployment": resolve_deployment(entry),
            "transport": first_descriptor_value(entry, "transport"),
            "has_installable_package": bool(first_descriptor_value(entry, "has_installable_package")),
            "has_remote": resolve_has_remote(entry),
            "attributes": first_descriptor_value(entry, "attributes") or [],
            "license": first_descriptor_value(entry, "license"),
            "added": entry.get("added"),
            # Backfilled by fetch_mcp_rankings.py, not part of the embedding
            # text -- doesn't affect content_hash, so a stars/downloads
            # refresh alone never forces a re-embed. See --rankings-only for
            # pushing a fresh value onto an already-indexed point cheaply.
            "stars": entry.get("stars"),
            "weekly_downloads": entry.get("weekly_downloads"),
            "monthly_downloads": entry.get("monthly_downloads"),
            "npm_dependents": entry.get("npm_dependents"),
            "npm_score_final": entry.get("npm_score_final"),
            "downloads_source": entry.get("downloads_source"),
            # GitHub's own repo-level language detection (fetch_mcp_rankings.py,
            # captured free off the same call as stars) -- "programming
            # language," separate from `registry_type` ("package manager": npm/
            # pypi/oci/nuget/cargo/... -- a package manager, not a language;
            # npm serves both JS and TS packages, so registry_type alone
            # can't stand in for language). package_manager below is just a
            # clearer alias of registry_type for payload consumers that want
            # this concept under an unambiguous name rather than inferring
            # it's "package manager" from a field literally called
            # registry_type.
            "language": entry.get("language"),
            "package_manager": first_descriptor_value(entry, "registry_type"),
            # Backfilled by fetch_mcp_security.py (OSV.dev) -- a clean
            # package is a real {0, []} result, not an absent field; absent
            # fields here mean "never scanned," not "no vulns found."
            "security_vuln_count": entry.get("security_vuln_count"),
            "security_vuln_ids": entry.get("security_vuln_ids"),
            "security_max_severity": entry.get("security_max_severity"),
            "security_source": entry.get("security_source"),
            # Direct dependencies only, not a full transitive tree -- see
            # fetch_mcp_security.py's "DEPENDENCY COVERAGE" docstring
            # section for exactly what this does and doesn't cover.
            "security_direct_deps_scanned": entry.get("security_direct_deps_scanned"),
            "security_direct_deps_vuln_count": entry.get("security_direct_deps_vuln_count"),
            "security_direct_deps_with_vulns": entry.get("security_direct_deps_with_vulns"),
            # Three independent "last checked" clocks -- deliberately not
            # collapsed into one "last_updated" field, since each reflects a
            # different fetch running on its own schedule against a
            # different upstream (download_readmes.py's readme pull vs.
            # fetch_mcp_rankings.py's two GitHub/npm phases vs.
            # fetch_mcp_security.py's OSV scan), and none of them were
            # actually reaching this payload before -- a real gap, not by
            # design: registry.json has always tracked all three, this
            # function just never surfaced them.
            "readme_updated": entry.get("readme_fetched"),
            "stars_updated": entry.get("stars_updated"),
            "downloads_updated": entry.get("downloads_updated"),
            "security_updated": entry.get("security_updated"),
        }


def existing_hashes(client) -> dict:
    hashes = {}
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, with_payload=["content_hash"], with_vectors=False, limit=1000, offset=offset
        )
        for p in points:
            hashes[p.id] = (p.payload or {}).get("content_hash")
        if offset is None:
            break
    return hashes


def existing_mcp_ids(client) -> dict[str, str]:
    """point_id -> mcp_id for every point currently indexed -- the fast
    default "already indexed" check (mirrors ../index_qdrant.py's
    known_paths()), no content read needed."""
    ids = {}
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION, with_payload=["mcp_id"], with_vectors=False, limit=1000, offset=offset
        )
        for p in points:
            mcp_id = (p.payload or {}).get("mcp_id")
            if mcp_id:
                ids[p.id] = mcp_id
        if offset is None:
            break
    return ids


RANKING_FIELDS = (
    "stars", "weekly_downloads", "monthly_downloads", "npm_dependents", "npm_score_final", "downloads_source",
    "language", "security_vuln_count", "security_vuln_ids", "security_max_severity", "security_source",
    "security_direct_deps_scanned", "security_direct_deps_vuln_count", "security_direct_deps_with_vulns",
    "stars_updated", "downloads_updated", "security_updated",
)
RANKING_OP_BATCH = 1000  # operations per batch_update_points call -- these
# carry no vectors, so a much larger batch than upsert's byte-capped chunks
# is safe; 1000 is just a round, comfortably-small number of ops per call.


def ensure_collection(client, *, rebuild: bool) -> None:
    """Create COLLECTION if it doesn't exist; with rebuild=True, delete it
    first regardless of whether it exists, then create fresh. This is the
    reusable, scriptable path for wiping and rebuilding the collection --
    see --rebuild's help text for why that matters: the collection was
    previously found contaminated with a different pipeline's data (skills
    payloads under the "mcp_servers" name) by a one-off manual client call
    during this pipeline's development, not through this function. Routing
    every rebuild through here from now on means it's always this same,
    reviewed schema -- never a fresh ad hoc snippet run once and forgotten."""
    if rebuild and client.collection_exists(COLLECTION):
        info = client.get_collection(COLLECTION)
        print(f"[rebuild] deleting existing collection={COLLECTION!r} ({info.points_count} point(s))")
        client.delete_collection(COLLECTION)

    if not client.collection_exists(COLLECTION):
        print(f"[rebuild] creating collection={COLLECTION!r}")
        client.create_collection(
            COLLECTION,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=client.get_embedding_size(MODEL_NAME), distance=models.Distance.COSINE
                ),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF),
            },
        )


def select_ranked_sample(registry_rows: list[dict], n: int) -> list[dict]:
    """Rows that already carry ranking data (fetch_mcp_rankings.py), highest
    GitHub stars first, npm-only rows (no stars) after -- the "small batch
    for human review before indexing everything" set, so the sample
    actually exercises the ranking payload fields end to end instead of
    picking arbitrary rows that might all have stars=None."""
    ranked = [r for r in registry_rows if r.get("stars") is not None or r.get("weekly_downloads") is not None]
    ranked.sort(key=lambda r: (r.get("stars") if r.get("stars") is not None else -1), reverse=True)
    return ranked[:n]


def sync_ranking_payload(client, registry_rows: list[dict]) -> int:
    """Push fresh stars/weekly_downloads/downloads_source onto points that
    are ALREADY indexed, via a batch of per-point SetPayloadOperations --
    no vectors touched, no re-embed, and doesn't go through content_hash at
    all (ranking data was deliberately kept out of it -- see load_points()).
    This is the cheap path for a ranking-only refresh between full
    re-index runs; a point not yet indexed picks up ranking data naturally
    whenever it's first embedded instead. Returns the number of points
    updated."""
    existing = existing_mcp_ids(client)  # point_id -> mcp_id
    by_registry_id = {r["id"]: r for r in registry_rows}

    ops = []
    updated = 0
    for point_id, registry_id in existing.items():
        entry = by_registry_id.get(registry_id)
        if entry is None:
            continue
        payload = {field: entry.get(field) for field in RANKING_FIELDS}
        # Not a literal field on entry (derived from a source descriptor,
        # same as load_points()) -- set separately rather than added to
        # RANKING_FIELDS, which assumes a direct entry.get() lookup.
        payload["package_manager"] = first_descriptor_value(entry, "registry_type")
        payload["readme_updated"] = entry.get("readme_fetched")
        glama_source = next((s for s in entry.get("sources", []) if s["type"] == "glama"), None)
        payload["glama_description"] = glama_source.get("description") if glama_source else None
        payload["readme_description"] = mcp_registry.extract_readme_description(readme_text(entry))
        ops.append(models.SetPayloadOperation(set_payload=models.SetPayload(payload=payload, points=[point_id])))
        updated += 1
        if len(ops) >= RANKING_OP_BATCH:
            client.batch_update_points(COLLECTION, update_operations=ops)
            ops = []
    if ops:
        client.batch_update_points(COLLECTION, update_operations=ops)
    return updated


def upload_in_batches(client, points, batch_size: int, embed_batch_size: int, embed_threads: int,
                       total: int | None = None) -> None:
    """`points` is an ITERATOR (typically a generator from load_points()),
    not a list -- deliberately never materialized in full before this
    function runs. Confirmed the hard way why that matters: each point
    payload carries a full README (up to tens of KB), and this pipeline's
    box has 3.7GB RAM total -- `list(load_points(all_81914_rows))` alone
    was observed pushing RSS past 1.3GB and climbing, on a box already
    under memory pressure from concurrent processes, well before a single
    point had been embedded or uploaded. Chunking `points` here (via
    itertools.islice, one `batch_size`-sized slice at a time) means at most
    one batch's worth of full README text is ever resident in memory,
    regardless of how many rows the whole run covers.

    `total`, if given, is only for the tqdm progress bar's ETA display --
    computing it from a materialized list would defeat the whole point of
    this function taking an iterator in the first place, so the caller is
    expected to have it cheaply (e.g. a registry row count) rather than
    this function deriving it from `points` itself."""
    dense_model = get_embedder(MODEL_NAME, sparse=False, threads=embed_threads)
    sparse_model = get_embedder(SPARSE_MODEL_NAME, sparse=True, threads=embed_threads)

    points_iter = iter(points)
    with tqdm(total=total, unit="server", desc="embedding", smoothing=0.1) as bar:
        while True:
            chunk = list(itertools.islice(points_iter, batch_size))
            if not chunk:
                break
            texts = [f"{p['name']}: {p['description']}\n\n{p['readme']}" for p in chunk]

            dense_vecs = list(dense_model.embed(texts, batch_size=embed_batch_size))
            sparse_vecs = list(sparse_model.embed(texts, batch_size=embed_batch_size))

            qdrant_points = [
                models.PointStruct(
                    id=p["id"],
                    vector={
                        DENSE_VECTOR_NAME: dense_vecs[j].tolist(),
                        SPARSE_VECTOR_NAME: models.SparseVector(
                            indices=sparse_vecs[j].indices.tolist(),
                            values=sparse_vecs[j].values.tolist(),
                        ),
                    },
                    payload=p,
                )
                for j, p in enumerate(chunk)
            ]
            upsert_size_capped(client, COLLECTION, qdrant_points)
            bar.update(len(chunk))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--hash", action="store_true",
        help="Diff every row by content hash instead of the default fast id-based check "
        "(catches a row whose readme/description changed at an id already indexed).",
    )
    parser.add_argument(
        "--rankings-only", action="store_true",
        help="Push fresh stars/weekly_downloads/language/security-scan fields onto already-indexed "
        "points via payload-only update (no embedding, no vector touch). Does not add "
        "newly-discovered rows -- run without this flag first if there are unindexed rows.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Points per upsert call, AND per in-memory chunk while streaming (default 500 -- lowered from an "
        "earlier 10000 default after that was observed pushing RSS past 1.3GB and climbing on this pipeline's "
        "3.7GB box; see upload_in_batches()'s docstring). Each point carries a full README (up to tens of KB), "
        "so this is the real memory-vs-throughput knob, not just an upsert-call-count one.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap points indexed, e.g. for a quick test run")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Delete the collection first (if it exists) and recreate it empty before indexing. "
        "Use when the collection is suspected contaminated/stale, not for routine re-indexing.",
    )
    parser.add_argument(
        "--sample-ranked", type=int, default=None, metavar="N",
        help="Index only the N rows that already have ranking data (highest stars first), instead of the "
        "full registry -- the small-batch-for-human-review step before a real --rebuild run indexes everything.",
    )
    parser.add_argument(
        "--ids", type=str, default=None,
        help="Comma-separated registry ids to index, instead of the full registry -- for indexing/reviewing "
        "a specific, targeted set of rows (e.g. after fetch_mcp_security.py --ids on the same set).",
    )
    parser.add_argument(
        "--no-prune", action="store_true",
        help="Never delete points whose registry id isn't in this run's row set. Implied by "
        "--ids/--sample-ranked (a deliberately narrowed run never represents the whole registry); "
        "this flag adds the same protection to an unflagged run.",
    )
    parser.add_argument("--embed-threads", type=int, default=DEFAULT_EMBED_THREADS)
    parser.add_argument("--embed-batch-size", type=int, default=DEFAULT_EMBED_BATCH_SIZE)
    args = parser.parse_args()

    client = get_client()
    ensure_collection(client, rebuild=args.rebuild)

    registry_rows = mcp_registry.load_registry()

    # `restricted` matters for pruning below: --ids/--sample-ranked narrow
    # registry_rows to a deliberate subset, but the default/--hash branches'
    # stale-point cleanup treats "not in registry_rows" as "removed from the
    # registry, delete it" -- which is correct against the FULL registry,
    # but wrong against a deliberately narrowed one (it would delete every
    # previously-indexed point outside this run's subset). Confirmed this
    # live: an --ids run against an already-populated collection pruned 50
    # points from an earlier --sample-ranked run that were never meant to be
    # touched. Fix is to simply skip pruning whenever this run is a
    # restricted subset, not to try to compute "staleness" against a set
    # that was never meant to represent the whole registry.
    restricted = bool(args.no_prune)
    if args.ids is not None:
        wanted = set(args.ids.split(","))
        registry_rows = [r for r in registry_rows if r["id"] in wanted]
        restricted = True
        print(f"[ids] restricting this run to {len(registry_rows)}/{len(wanted)} requested row(s) found in registry")
    elif args.sample_ranked is not None:
        registry_rows = select_ranked_sample(registry_rows, args.sample_ranked)
        restricted = True
        print(
            f"[sample-ranked] restricting this run to {len(registry_rows)} row(s) with existing ranking data "
            f"-- review the result before running a full (unflagged) index."
        )

    if args.rankings_only:
        updated = sync_ranking_payload(client, registry_rows)
        print(f"Synced ranking payload (stars/weekly_downloads) onto {updated} already-indexed point(s).")
        return

    if args.hash:
        # Streams load_points() once rather than materializing every point
        # up front -- each point carries a full README (up to tens of KB),
        # and this pipeline's box has 3.7GB RAM total. An unchanged point's
        # full dict (readme text included) is discarded immediately after
        # its hash is checked, never retained -- only `changed` (the
        # subset actually needing re-embedding) stays resident, same
        # memory discipline as the default branch below and
        # upload_in_batches() itself. See upload_in_batches()'s docstring
        # for the incident that made this matter.
        old_hashes = existing_hashes(client)
        changed: list[dict] = []
        current_point_ids: set[str] = set()
        total_points = 0
        for i, p in enumerate(load_points(registry_rows)):
            if args.limit is not None and i >= args.limit:
                break
            total_points += 1
            current_point_ids.add(p["id"])
            if old_hashes.get(p["id"]) != p["content_hash"]:
                changed.append(p)

        stale_ids = [] if restricted else [pid for pid in old_hashes if pid not in current_point_ids]

        if stale_ids:
            client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale_ids))
        if changed:
            upload_in_batches(
                client, changed, args.batch_size, args.embed_batch_size, args.embed_threads, total=len(changed)
            )

        print(
            f"Indexed {total_points} MCP server(s) into collection={COLLECTION!r}: "
            f"{len(changed)} new/changed, {len(stale_ids)} removed, {total_points - len(changed)} unchanged"
        )
    else:
        existing = existing_mcp_ids(client)
        known_registry_ids = set(existing.values())
        current_registry_ids = {r["id"] for r in registry_rows}

        # Generator, not a materialized list -- see upload_in_batches()'s
        # docstring. `new_count` is computed from id membership only (no
        # readme text touched) so the tqdm total/print statement don't need
        # the list realized either.
        new_points_gen = load_points(registry_rows, skip_ids=known_registry_ids)
        new_count = sum(1 for r in registry_rows if r["id"] not in known_registry_ids)
        if args.limit is not None:
            new_points_gen = itertools.islice(new_points_gen, args.limit)
            new_count = min(new_count, args.limit)

        stale_point_ids = [] if restricted else [pid for pid, rid in existing.items() if rid not in current_registry_ids]
        if stale_point_ids:
            client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale_point_ids))

        if new_count:
            upload_in_batches(
                client, new_points_gen, args.batch_size, args.embed_batch_size, args.embed_threads, total=new_count
            )

        print(
            f"Indexed {new_count} new MCP server(s) by id check "
            f"(skipped {len(known_registry_ids)} already-known id(s)); pruned {len(stale_point_ids)} stale point(s). "
            f"Use --hash to also catch rows whose readme/description changed at an already-indexed id."
        )


if __name__ == "__main__":
    main()
