# Daily/recurring maintenance job

This is the recurring workflow for keeping `repo-seeds/registry.json` (the
pipeline's single source of truth) curated and the search index fresh. It's
written as four steps you can run in order, by hand or from a script/cron —
none of them require re-explaining the pipeline itself (see
[`README.md`](README.md#pipeline) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for that).

**[`RUN.sh`](RUN.sh) automates steps 3 (partially) and 4 of this workflow**
— repo discovery (`sync-seed`, marketplace, optional GitHub search/leaderboard)
and rerunning the pipeline, in one command. It does **not** automate steps 0-2
(reviewing what's unsynced, skipping noisy repos, blacklisting bad skills) —
those stay a human judgment call by design. Run `./RUN.sh` for the mechanical
part of a daily/recurring pass, then still walk steps 0-2 below yourself.

## 0. Check what didn't sync

Every registry entry carries a `last_synced` timestamp, stamped by
`batch_pipeline.py` per-batch (or `registry.py mark-synced`, called by
`archived/run_pipeline.sh`'s last step) on every repo that has a directory
under `repos/` once extract+index have both succeeded — i.e. "cloned AND
through RAG". Start here before reviewing:

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

**Every source of skills has to be continually refreshed — this isn't
just a `sync-seed` thing.** Two different kinds of staleness are in play
here, and it's easy to fix one and still be silently stuck on the other:

1. **Registry staleness** — a repo already tracked hasn't been re-cloned
   recently (`last_synced`, step 0/4 handle this).
2. **Source staleness** — the *list a repo would be discovered from* is
   itself out of date, so a brand-new upstream repo is invisible no matter
   how often you re-run the discovery step against it.

(2) is the one that's easy to miss, because the discovery scripts all exit
`0` and print "0 new repos" whether nothing changed upstream or your local
view of upstream is just stale. Concretely, for `officialskills.sh`:
[`repo-seeds/awesome-agent-skills/README.md`](repo-seeds/awesome-agent-skills/README.md)
is a **vendored, point-in-time copy** of the upstream
[VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)
repo, tracked separately in
[`repo-seeds/repo_seeds.json`](repo-seeds/repo_seeds.json) (`last_pulled`,
distinct from any individual repo's `last_synced` in `registry.json`).
`registry.py sync-seed` only ever regex-scrapes github.com links out of
*that vendored copy* — it never talks to GitHub itself. If the vendored
copy is a week stale, `sync-seed` will happily run every day, exit `0`,
and never surface the six repos VoltAgent added upstream in the meantime.
**`refresh_seeds.py` is what actually re-clones the upstream repo and
overwrites the vendored copy** — run it before `sync-seed`, not instead of
it:

```bash
./refresh_seeds.py          # re-vendor every tracked seed list from upstream
./registry.py sync-seed      # THEN scrape the now-fresh vendored copy
./registry.py seeds          # check every seed list's last_pulled
```

`RUN.sh` chains `refresh_seeds.py` → `sync-seed` in that order automatically
every run, for exactly this reason.

The same "is the *source* itself fresh, not just the registry" question
applies to every channel, with a different answer per channel:

| Source | What can go stale | How it's refreshed |
|---|---|---|
| Seed lists (`officialskills.sh`, ...) | The vendored copy under `repo-seeds/` | `refresh_seeds.py`, run before `sync-seed` (see above) |
| Marketplace | Nothing — fetched live from Anthropic's repo every run | No separate refresh step needed; `fetch_marketplace.py` always gets current data |
| `skills.sh` leaderboard | The raw snapshot in `leaderboard-raw/` | `pull_leaderboard.py`, **manual-only** (see [step 4](#4-rerun-the-pipeline-clone--extract--index) and `RUN.sh`'s header) — `add_skillsh_leaderboard.py` only reads whatever snapshot already exists, same relationship `sync-seed` has to `refresh_seeds.py` |
| GitHub search | Nothing — each run queries the live API | No separate refresh step; running `search_github.py` again IS the refresh |
| Individual repos already in the registry | Their own clone on disk | `last_synced` / step 0 above, refreshed by `batch_pipeline.py` |

`RUN.sh` runs (a) and (b) below automatically every time (plus
`refresh_seeds.py` immediately before (b), per the above); (c) and (d) are
opt-in/manual since they need human judgment (c) or are inherently one-off
(d) — see `RUN.sh`'s header comment. Four independent ways new repos enter
the registry, all additive-only — none of them ever remove an existing
registry row:

```bash
# a) Anthropic's official Claude plugin marketplace -- no review needed,
#    it's already a curated Anthropic source
./fetch_marketplace.py

# b) The vendored awesome-agent-skills list -- picks up anything appended
#    to repo-seeds/awesome-agent-skills/README.md since the last sync.
#    Run refresh_seeds.py first (see above) or this only sees whatever
#    was vendored as of the last refresh, not what's upstream right now.
./refresh_seeds.py
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
python3 batch_pipeline.py --batch-size 100 --only-unsynced --stats
```

This is the standard way to rerun the pipeline now, for any size run.
`clone_repos.py` on its own clones *everything* matching into `repos/` (full
git clones) before `extract_search_raw.py` ever runs — for the full
registry that's several GB+ sitting on disk at once, and has filled the
disk before. `batch_pipeline.py` clones in bounded batches, extracts each
batch into `search-raw/` (the only thing that actually needs to persist),
then deletes `repos/` via `clean_repos.sh` before the next batch — so
`repos/` never holds more than one batch's clones regardless of registry
size.

- `--only-unsynced` limits it to repos step 0's `unsynced` check would
  flag, and skips by content (not registry position) so it correctly picks
  up the remaining backlog on a resumed run.
- `--stats` logs a running `stats.py` snapshot to `stats.log` after every
  batch, so you can watch the sync/index/CSV counts climb instead of
  waiting on one big opaque run.
- `--batch-size N` (default 100) — smaller for a slow/careful catch-up or
  debugging, larger if the delta is small and you just want it done.

It's safe and cheap to run multiple times a day:

- **`clone_repos.py`** (invoked once per batch) — has its own per-repo 24h
  skip (`.clone_state.json`); repos cloned within the last day are skipped
  with no GitHub API call at all, so re-running costs almost nothing beyond
  newly-added repos.
- **`extract_search_raw.py`** — a full rescan of the *current batch's*
  `repos/` every time (not incremental across the whole registry), so it's
  seconds even at thousands of files.
- **`index_qdrant.py`** — incremental (hashes each file's content, only
  re-embeds new/changed files, and removes points for files that
  disappeared from `search-raw/`), so a same-day rerun with nothing new
  finishes almost instantly.
- **`mark_synced_pairs()`** (called internally by `batch_pipeline.py` after
  each batch's extract+index) — stamps `last_synced` for exactly that
  batch's confirmed-cloned repos. This is what step 0's `unsynced` check
  reads, so a run that dies mid-batch only leaves *that* batch's repos
  unstamped, not the whole run.

`fetch_marketplace.py` (picking up new repos from the marketplace) is no
longer chained into this step automatically — run it separately as part of
[step 3](#3-update-the-repo-list-always-additive-never-deletes) before
rerunning the pipeline if you want fresh marketplace repos included.

**Legacy: `archived/run_pipeline.sh`.** The original single-shot pipeline
runner (fetch marketplace → clone everything → extract → index →
mark-synced) has been moved to `archived/` — it clones the *entire*
matching set into `repos/` in one shot with no batching, which is exactly
the disk-filling failure mode `batch_pipeline.py` was built to avoid. Kept
around for reference only; don't run it as part of the regular workflow.

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
  support concurrent writers.** If you (or a cron job) run `index_qdrant.py`
  / `batch_pipeline.py` while another instance is already running, the
  second one crashes with `RuntimeError: Storage folder ... is already
  accessed by another instance of Qdrant client`. Make sure
  `batch_pipeline.py` invocations don't overlap (e.g. a cron job that takes
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
