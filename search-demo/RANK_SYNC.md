# Rank Sync

How popularity/ranking data (`skills.sh` leaderboard rank + install count,
GitHub search order rank) is captured, stored, refreshed, and surfaced —
independent of the clone/extract/index pipeline.

## Data model

Ranking data lives in `repo-seeds/registry.json`, on the **source
descriptor**, not on the repo row itself and not on individual skills. Each
registry row is one repo; `sources[]` is a list of descriptors, one per
discovery channel (see `registry.py` module docstring for the full list of
channel types). Only two channel types carry ranking data today:

- `"skills.sh"` descriptor: `rank` (lower = better), `skill_count` (how many
  of that repo's skills appear on the leaderboard), `top_installs` (highest
  install count among them), `rank_last_updated` (ISO date this descriptor's
  numbers were last refreshed).
- `"search"` descriptor: `rank` (1-based position in that specific
  `search_github.py` run — `query` + `sort` + `exact` together identify
  *which* search it came from), `rank_last_updated`.

**Each repo is a separate source.** If the same skill (by content) is
vendored into two different repos, each repo gets its own registry row, its
own `"skills.sh"`/`"search"` descriptors, and its own rank — there is no
single "rank of a skill," only "rank of the repo that skill was found in,"
per channel. A skill's search results (`locations[]` in the Qdrant payload)
show every repo it lives in, each carrying its own independent ranking.

**GitHub search rank applies to the whole repo, not one skill.** Rank comes
from `search_github.py`'s search-result ordering for a repo, so every
skill found under that repo inherits the same `search_rank` value — there's
no finer-grained "this skill ranked higher than that skill within the same
repo" concept.

## How it reaches Qdrant/CSV/Streamlit

`index_qdrant.py` builds a single composite `ranking` string per skill
location, via `_ranking_string()`: every numeric field on every source
descriptor for that skill's repo, namespaced `{source_type}_{field}=value`,
space-separated, sorted. Example:

```
search_rank=69 skills_sh_rank=687 skills_sh_skill_count=144 skills_sh_top_installs=25864
```

This is generic over an **N sources × N stats** matrix — a new source type
with its own numeric fields (e.g. an npm download count) shows up
automatically with zero code changes, as long as its registry descriptor
carries plain numeric fields.

- Per-copy: `locations[].ranking` (one per repo the skill is vendored in).
- Flattened: top-level `ranking` on the point, taken from the
  highest-starred copy (same rule `stars`/`owner`/`repo` already use).
- `export_csv.py` includes `ranking` as a column in `skills_export.csv`.

## Refreshing rank data — separate from clone/index

Updating a repo's rank never requires re-cloning it or having it on disk.

### skills.sh

Bulk (refresh every tracked repo from a leaderboard snapshot):

```
python3 pull_leaderboard.py 10000        # optional -- only if you need a fresher snapshot than leaderboard-raw/combined.json
python3 add_skillsh_leaderboard.py        # re-upserts rank/skill_count/top_installs + rank_last_updated for every repo it finds
```

Single repo (you already know the numbers, e.g. from a manual leaderboard
check — no network call):

```
./registry.py update-skillsh owner/repo --rank 687 --skill-count 144 --top-installs 25864
```

Both paths only touch `registry.json` and stamp `rank_last_updated`.

### GitHub search rank

Re-run the same query/sort/exact combination that originally found the
repo (check its existing `"search"` descriptor in `registry.json` for
those three values), then re-approve:

```
./search_github.py "agents skills" --exact --sort best-match --top 100 --format json --out repo-seeds/github_search_results.json
./registry.py add-search repo-seeds/github_search_results.json --approve owner/repo --approve owner2/repo2 ...
```

`add-search` → `upsert()` updates the existing `"search"` descriptor in
place (new `rank` + `rank_last_updated`), it does not create a duplicate.
Repos that fall outside `--top N` in the fresh run simply keep their old
`rank`/`rank_last_updated` untouched (not zeroed out) — bump `--top` if a
repo you care about is no longer appearing.

**Pitfall: don't restrict `--approve` to repos that already have a `search`
descriptor.** A repo discovered first via `seed`/`marketplace`/`manual` can
still legitimately match the search query and deserves a `search` rank too
— filtering the approve list down to "already has a `search` source" will
silently skip it forever, even though it's sitting right there in the fresh
results (e.g. `obra/superpowers` ranked #7 but was never approved into
`search` because it was seed/marketplace-first). Match the fresh results
against *every* already-tracked repo (`registry.py list`), not just repos
that happen to carry a `search` descriptor already:

```python
tracked = {(r["owner"].lower(), r["repo"].lower()) for r in registry.load_registry()}
to_approve = [r["full_name"] for r in results["results"]
              if (r["owner"].lower(), r["repo"].lower()) in tracked]
```

### Push refreshed rank into Qdrant/search results/CSV

```
python3 index_qdrant.py --metadata-only
python3 export_csv.py            # optional, if you also want skills_export.csv regenerated
```

`--metadata-only` re-derives `stars`/`sources`/`ranking` for every point
already in the collection straight from `registry.json` and does a
payload-only `set_payload` push — no re-embedding, no read from
`search-raw/` or `repos/`. This works even if the repo's clone under
`repos/<owner>/<repo>` has since been deleted, as long as the point is
still indexed in Qdrant.

## `rank_last_updated`

Every `"skills.sh"` and `"search"` source descriptor carries its own
`rank_last_updated` (ISO date), stamped whenever that descriptor's rank
fields are written — by `update_skillsh()`, `add_skillsh_leaderboard.py`,
or `add_search_results()`. It answers "how stale is this repo's rank," per
channel, independent of `last_synced` (which tracks clone/extract/index
freshness, not rank freshness — a repo can be rank-fresh and clone-stale,
or vice versa).
