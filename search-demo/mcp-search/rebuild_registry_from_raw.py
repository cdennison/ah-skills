#!/usr/bin/env python3
"""Rebuild mcp-repo-seeds/registry.json from scratch by replaying the
cached raw pull dumps (mcp-search-raw/official_registry.json,
mcp-search-raw/glama.json) through the REAL, unmodified
pull_official_registry.upsert_entry()/pull_glama.upsert_entry() functions
-- no network calls, deterministic, and produces exactly the structure a
fresh `pull_official_registry.py && pull_glama.py` run would have (same
dedup/identity-resolution/source-corroboration logic, since it's the same
code, not a reimplementation).

Exists because registry.json can be lost in a way the raw dumps can't:
confirmed the hard way -- mcp_registry.save_registry()'s
REGISTRY_FILE.write_text(...) truncates the file before writing, with no
signal handler guarding that window, so an interrupted write (a supervised
job's process getting SIGTERM'd mid-save) can leave registry.json
completely empty. The raw dumps are the recovery path for everything
except awesome-mcp-servers-only rows (pull_seed_repo.py has no raw-dump
cache -- it scans repos live, so rows found ONLY through that source with
no official_registry/glama corroboration need a real pull_seed_repo.py
re-run, not just a replay) and any enrichment layered on top afterward
(readme_path/mcp_category/stars/downloads/security -- re-run
download_readmes.py --no-clone, classify_mcp_registry.py,
fetch_mcp_rankings.py, fetch_mcp_security.py in that order after this).

Refuses to run against a non-empty registry.json unless --force is passed
-- this is a rebuild-from-scratch tool, not a merge, and accidentally
replaying it over live data would silently re-derive `description` fields
per upsert()'s current priority rules, which could differ from
whatever hand-curated state exists on top of a working registry.

Usage:
    python rebuild_registry_from_raw.py            # refuses if registry.json is non-empty
    python rebuild_registry_from_raw.py --force     # rebuild anyway, overwriting current registry.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
import pull_glama
import pull_official_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Rebuild even if registry.json currently has rows")
    args = parser.parse_args()

    try:
        existing = mcp_registry.load_registry()
    except RuntimeError:
        # Exactly the case this script exists for: registry.json exists but
        # is empty/corrupt (see mcp_registry.load_registry()'s docstring).
        # Proceed to rebuild rather than treating this as "refuse."
        existing = []
    if existing and not args.force:
        print(
            f"[refusing] registry.json already has {len(existing)} row(s) -- this tool rebuilds from scratch, "
            f"it doesn't merge. Pass --force to overwrite anyway.",
            file=sys.stderr,
        )
        sys.exit(1)

    registry: list[dict] = []
    index: dict[str, dict] = {}

    official_path = mcp_registry.RAW_DIR / "official_registry.json"
    if official_path.exists():
        items = json.loads(official_path.read_text())
        print(f"[official_registry] replaying {len(items)} cached entries...")
        ok = failed = 0
        for item in items:
            try:
                pull_official_registry.upsert_entry(registry, item, index)
                ok += 1
            except Exception as e:
                print(f"  [warn] failed to replay one entry: {e!r}", file=sys.stderr)
                failed += 1
        print(f"[official_registry] {ok} ok, {failed} failed")
    else:
        print(f"[official_registry] no raw dump found at {official_path} -- skipped")

    glama_path = mcp_registry.RAW_DIR / "glama.json"
    if glama_path.exists():
        items = json.loads(glama_path.read_text())
        print(f"[glama] replaying {len(items)} cached entries...")
        ok = failed = 0
        for item in items:
            try:
                pull_glama.upsert_entry(registry, item, index)
                ok += 1
            except Exception as e:
                print(f"  [warn] failed to replay one entry: {e!r}", file=sys.stderr)
                failed += 1
        print(f"[glama] {ok} ok, {failed} failed")
    else:
        print(f"[glama] no raw dump found at {glama_path} -- skipped")

    # Re-associate already-downloaded readme files -- free (no network,
    # pure disk check), and worth doing here rather than leaving it to
    # download_readmes.py: that script decides what needs fetching purely
    # from registry.json's readme_fetched/readme_last_attempt timestamps,
    # which this rebuild can't restore (not in the raw dumps) -- run it
    # blind against a freshly-rebuilt registry and it would re-download
    # every readme over the network from scratch, even the ~71K already
    # sitting on disk untouched by the data loss this script recovers from.
    reassociated = 0
    for row in registry:
        repo_url = row.get("repo_url")
        if not repo_url or not mcp_registry.parse_github_repo_url(repo_url):
            continue
        path = mcp_registry.readme_path_for(repo_url)
        if path.exists():
            mcp_registry.mark_readme(registry, row["id"], path, "recovered-from-disk", index=index)
            reassociated += 1
    mcp_registry.save_registry(registry)
    print(f"[readme reassociation] {reassociated} row(s) matched to an already-downloaded readme file on disk")

    print(f"\nrebuilt {len(registry)} row(s) into {mcp_registry.REGISTRY_FILE}")
    print(
        "STILL NOT recovered by this: awesome-mcp-servers-only rows (no raw-dump cache -- re-run "
        "pull_seed_repo.py), readme_path for rows whose file genuinely isn't on disk (re-run "
        "download_readmes.py --no-clone for the remainder), mcp_category (re-run "
        "classify_mcp_registry.py), and all stars/downloads/security enrichment (re-run "
        "fetch_mcp_rankings.py / fetch_mcp_security.py from scratch)."
    )


if __name__ == "__main__":
    main()
