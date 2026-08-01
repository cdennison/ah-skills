# Daily/recurring maintenance job

This is the recurring workflow for keeping `repo-seeds/registry.json` (the
pipeline's single source of truth) curated and the search index fresh. It's
written as four steps you can run in order, by hand or from a script/cron —
none of them require re-explaining the pipeline itself (see
[`README.md`](README.md#pipeline) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for that).

## 0. Check what didn't sync

Every registry entry carries a `last_synced` timestamp, stamped by
`run_pipeline.sh`'s last step (`registry.py mark-synced`) on every repo that
has a directory under `repos/` once extract+index have both succeeded — i.e.
"cloned AND through RAG". Start here before reviewing:

```bash
./registry.py unsynced   # active repos not synced today (never synced, or stale)
```

A repo failing to sync (clone error, 404, etc.) also gets `last_sync_failure`
+ `last_sync_failure_reason` recorded — by `clone_repos.py` itself when the
`git clone` fails, or generically by `mark-synced` if a repo is missing from
disk for some other reason. `last_synced` is only ever touched by a
*successful* sync, so a repo that fails today keeps whatever `last_synced`
it last had (or none, if it's never succeeded) — the failure fields are
purely additive information, never a replacement for the success timestamp.
Check `last_sync_failure_reason` on anything `unsynced` turns up before
deciding whether to skip it (step 1) or investigate further.

## 1. Review the registry and mark low-quality repos "skip"

```bash
./registry.py list                       # eyeball everything
./registry.py skip owner/repo "reason, e.g. user feedback: mostly noise, not real skills"
./registry.py unskip owner/repo          # if you change your mind
```

Repos are **never deleted** by this review step — only marked
`status: "skip"` with a required reason, so the decision (and who/why) is
preserved. `registry.py remove` still exists, but reserve it for outright
mistakes (a typo'd owner/repo that was never real), not quality judgments.

**⚠️ Known gap: `skip` is currently schema-only.** `clone_repos.py`'s
`repo_pairs()` does **not** filter out `status: "skip"` entries yet, so
marking something skipped has *no effect* on cloning, extraction, or
indexing today. It's there to start capturing the decision now (e.g. from
recurring user feedback) so a future change to `repo_pairs()` can act on the
backlog of decisions already made. Don't rely on `skip` to actually reduce
what gets cloned/indexed until that's wired up.

## 2. Blacklist individual skills within a repo

Sometimes a whole repo is worth keeping but one specific skill in it isn't
(mislabeled, broken, low-quality, or user-reported as bad). This is a
separate mechanism from registry skip, and **is fully enforced**:

```bash
./blacklist.py add owner/repo/skills/some-skill/SKILL.md "reason, e.g. user feedback: irrelevant to our use case"
./blacklist.py remove owner/repo/skills/some-skill/SKILL.md
./blacklist.py list
```

`extract_search_raw.py` skips any blacklisted path when copying into
`search-raw/`, and deletes it from `search-raw/` if it was already copied
there before being blacklisted. `index_qdrant.py`'s existing hash-diff logic
then removes it from Qdrant on its next run (a file disappearing from
`search-raw/` looks the same to it whether the repo changed upstream or a
human blacklisted it).

**This only takes effect after you rerun `extract_search_raw.py` +
`index_qdrant.py`** (step 4) — it's not a live filter on the already-built
`qdrant_db/`.

## 3. Update the repo list (always additive, never deletes)

Four independent ways new repos enter the registry, all additive-only —
none of them ever remove an existing registry row:

```bash
# a) Anthropic's official Claude plugin marketplace -- no review needed,
#    it's already a curated Anthropic source
./fetch_marketplace.py

# b) The vendored awesome-agent-skills list -- picks up anything appended
#    to repo-seeds/awesome-agent-skills/README.md since the last sync
./registry.py sync-seed

# c) GitHub search -- requires a human review step before anything is added
./search_github.py "agents skills" --exact --format json --top 25 \
    --out repo-seeds/github_search_results.json
# (read repo-seeds/github_search_results.json yourself, then:)
./registry.py add-search repo-seeds/github_search_results.json \
    --approve owner/repo --approve owner2/repo2

# d) One-off manual add, always with a reason
./registry.py add-manual owner/repo "found it linked from a blog post"
```

All four are safe to run repeatedly (idempotent), and — importantly — none
of them skip a repo just because it's *already in the registry from a
different channel*. **Overlap between channels is expected and wanted, not
something to dedupe away.** Each registry row has a `sources` list; a repo
already tracked via `seed` that also turns up in the marketplace gets a
second, `marketplace` descriptor added to the *same* row (`registry.py
list` then shows it as `[seed+marketplace]`) — it does not get skipped, and
it does not get a duplicate row. `fetch_marketplace.py` and `registry.py
sync-seed` both print which repos were genuinely new vs. newly-overlapping
so you can see this happening:

```
$ ./fetch_marketplace.py
obra/superpowers already tracked -- also found in marketplace as 'superpowers' (now seed+marketplace)
0 new repo(s), 9 newly-overlapping repo(s)
```

Re-running the same channel again on a repo it already found (e.g. running
`fetch_marketplace.py` twice in a row) just refreshes that one descriptor's
detail in place — it doesn't add a second `marketplace` entry to the list,
and it never touches descriptors from *other* channels, or a repo's `skip`
status.

**This overlap tracking is repo-level bookkeeping only — it has zero effect
on cloning or indexing volume.** `clone_repos.py` clones each `owner/repo`
exactly once no matter how many sources list it (the registry has one row
per repo, full stop), and `extract_search_raw.py`/`index_qdrant.py` produce
exactly one Qdrant point per `SKILL.md` path found on disk, regardless of
how many registry sources led to that repo being cloned. `sources` answers
"where did we hear about this from," never "how many times is this
indexed" — that's always once.

## 4. Rerun the pipeline (clone → extract → index)

```bash
./run_pipeline.sh
```

This is safe and cheap to run multiple times a day:

- **`fetch_marketplace.py`** — one HTTP GET, always fast.
- **`clone_repos.py`** — has its own per-repo 24h skip
  (`.clone_state.json`); repos cloned within the last day are skipped with
  no GitHub API call at all, so re-running costs almost nothing beyond
  newly-added repos.
- **`extract_search_raw.py`** — a full rescan of `repos/` every time (not
  incremental), but this is a filesystem walk + copy, not network- or
  embedding-bound, so it's seconds even at ~12k files.
- **`index_qdrant.py`** — incremental (hashes each file's content, only
  re-embeds new/changed files, and removes points for files that
  disappeared from `search-raw/`), so a same-day rerun with nothing new
  finishes almost instantly.
- **`registry.py mark-synced`** — the final step; stamps `last_synced` on
  every repo now present in `repos/`. This is what step 0's `unsynced`
  check reads, so a `run_pipeline.sh` run that dies before this step (e.g.
  a crash mid-`index_qdrant.py`) leaves `last_synced` stale even for repos
  that were actually re-cloned that run.

## Non-obvious issues / things that can bite you

- **Registry `skip` does nothing yet (see step 1).** If you're trying to
  reduce clone/disk/index load by skipping noisy repos, it won't — until
  `repo_pairs()` in `registry.py` is updated to filter on `status`, skipped
  repos are still cloned, extracted, and indexed exactly as before.
- **Skipping a repo does not delete anything already on disk.** Even once
  skip filtering is implemented, the design decision (per earlier
  discussion) was to leave already-cloned `repos/<owner>/<repo>` directories
  alone rather than auto-deleting them — so disk usage under `repos/` only
  grows over time regardless of skip status, unless someone manually
  cleans it up.
- **Blacklisting requires a rerun to take effect** (see step 2) — it's not
  a live query-time filter.
- **`qdrant_db/` is a local embedded store, not a server — it does not
  support concurrent writers.** If you (or a cron job) run
  `index_qdrant.py` / `run_pipeline.sh` while another instance is already
  running, the second one crashes with `RuntimeError: Storage folder ...
  is already accessed by another instance of Qdrant client`. Make sure
  `run_pipeline.sh` invocations don't overlap (e.g. a cron job that takes
  longer than its own interval).
- **`fetch_marketplace.py` always clones each plugin's repo default branch**
  (`--depth 1`, via `clone_repos.py`), **not** the specific `ref`/`sha`/
  `commit` a plugin manifest entry pins to. That pinned metadata is kept on
  the registry entry for reference, but the actual clone can drift from
  what a given marketplace plugin version specifies.
- **A handful of marketplace plugins have no dedicated repo** — their
  `source` is a bare local path like `./plugins/x`, meaning the plugin lives
  inside the marketplace repo itself. `fetch_marketplace.py` resolves these
  via the plugin's `homepage` URL when possible, falling back to
  `anthropics/claude-plugins-public` (every case seen so far). If Anthropic
  restructures that repo, or a future plugin has neither a resolvable
  homepage nor lives in that fallback repo, `fetch_marketplace.py` prints a
  `[warn] could not resolve a repo for ...` line — check for that in the
  cron logs occasionally.
- **`extract_search_raw.py` has no general staleness cleanup** — if a
  repo is deleted from GitHub, or a `SKILL.md` is renamed/removed upstream,
  the old copy under `search-raw/` is *not* automatically removed on
  rescan unless it's specifically blacklisted (blacklist removal is the one
  case that's handled). This is a pre-existing gap, not something this
  daily job fixes.
