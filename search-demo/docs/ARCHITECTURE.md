# Architecture diagram

- `architecture.mmd` — the source of truth. A [Mermaid](https://mermaid.js.org/)
  flowchart describing every file/directory in the pipeline and how they
  connect (repo discovery → `repo-seeds/registry.json` → `clone_repos.py` →
  `repos/` → ... → `query.py` / `app/streamlit_app.py`), plus the
  ranking/leaderboard side flow, driven end to end by
  [`../batch_pipeline.py`](../batch_pipeline.py) — it clones in bounded
  batches and deletes `repos/` between them (via
  [`../clean_repos.sh`](../clean_repos.sh)) instead of accumulating full
  git clones for the whole registry at once. The original single-shot
  runner, `../archived/run_pipeline.sh`, is kept for reference only (see
  the diagram's "LEGACY" node) — it clones everything into `repos/` in one
  pass with no batching, which is the disk-filling failure mode
  `batch_pipeline.py` replaced it to avoid. For the recurring
  curation/maintenance workflow this diagram feeds into, see
  [`../DAILY_JOB.md`](../DAILY_JOB.md).

  Repo-discovery sources currently in the diagram:

  | Source | Status |
  |---|---|
  | `awesome-agent-skills/README.md` (vendored awesome-list) | implemented (`registry.py sync-seed`) |
  | `search_github.py` + human review | implemented (`registry.py add-search`) |
  | hand-picked repos | implemented (`registry.py add-manual`) |
  | `fetch_marketplace.py` (Anthropic's official Claude plugin marketplace) | implemented |
  | skills.sh / officialskills.sh | **planned, not implemented** — surveyed in [`TRD.md`](TRD.md) as a competitor registry worth indexing from, but there's no fetch script for it yet; shown dashed in the diagram |
  | [sickn33/agentic-awesome-skills](https://github.com/sickn33/agentic-awesome-skills) | **planned, not implemented as an aggregator** — currently just one `manual` row in the registry (a single repo), not yet scraped the way `awesome-agent-skills/README.md` is via `registry.py sync-seed`; shown dashed in the diagram |

  The diagram also covers `skills_map.py`, which produces
  `repo-seeds/skills.json` — a skill-name-keyed map to every repo (and that
  repo's registry sources) the skill was found in, so a skill vendored into
  more than one repo is visible instead of producing disconnected,
  look-alike results. It's refreshed from `clone_repos.py`'s main loop on
  every repo it processes, cloned or skipped, since a repo's registry
  sources can change between runs even when its content on disk doesn't.
- `architecture.jpg` — a rendered image of `architecture.mmd`, embedded in
  [`../README.md`](../README.md#architecture) so it also displays for
  viewers whose Markdown renderer doesn't support Mermaid fences.

**The two must be kept in sync by hand.** GitHub renders `.mmd`/Mermaid
fences live, but the `.jpg` does not auto-update — whenever you edit
`architecture.mmd` (new script, new directory, new consumer, etc.),
regenerate `architecture.jpg` in the same change.

## Layout: flowchart TD, not LR

The diagram uses `flowchart TD` (top-down). It was `LR` (left-right)
originally but that packed 20+ nodes with multi-line labels into one
horizontal band, which came out illegible at any resolution — the problem
was layout density, not pixel count. **Keep it `TD`** as the diagram grows;
switching back to `LR` will reintroduce the same problem.

## Regenerating architecture.jpg

**Use mermaid-cli (Option B below) as the default.** mermaid.ink (Option A)
was tried first and produced a blurry, hard-to-read image even at width=3000
— the public service appears to rasterize at a fixed internal resolution
regardless of the requested `width`, so it doesn't scale detail on a dense
diagram. mermaid-cli renders with real headless Chrome and respects `-s`
(scale), which is what actually fixed legibility.

**Option A — mermaid.ink (no install required, lower quality)**

Only use this as a fallback when a local Chrome isn't available (e.g. a
sandboxed CI step). Sends the diagram text to the public
[mermaid.ink](https://mermaid.ink) service — fine for this non-sensitive
diagram, but expect noticeably blurrier text than Option B.

```bash
cd search-demo
base64_input=$(base64 < docs/architecture.mmd | tr -d '\n')
curl -sL "https://mermaid.ink/img/${base64_input}?bg=white&width=3000" -o docs/architecture.jpg
```

**Option B — mermaid-cli (local, sharp text) — preferred**

Requires [`@mermaid-js/mermaid-cli`](https://github.com/mermaid-js/mermaid-cli)
(`brew install mermaid-cli` or `npm install -g @mermaid-js/mermaid-cli`) and
a headless Chrome. If `mmdc` reports it can't find Chrome, install one and
point `mmdc` at it explicitly — relying on the default cache path lookup
has been flaky:

```bash
npx puppeteer browsers install chrome-headless-shell
# note the printed install path, e.g.:
# /Users/you/.cache/puppeteer/chrome-headless-shell/mac_arm-<version>/chrome-headless-shell-mac-arm64/chrome-headless-shell
```

`mmdc` only writes `.md`/`.svg`/`.png`/`.pdf`, so render to PNG and convert.
`-s 3` (scale factor) is what makes the text sharp — don't drop it:

```bash
cd search-demo
PUPPETEER_EXECUTABLE_PATH=<path from the install step above> \
  mmdc -i docs/architecture.mmd -o docs/architecture.png -b white -w 2400 -s 3
sips -s format jpeg docs/architecture.png --out docs/architecture.jpg   # macOS
rm docs/architecture.png
```

(On Linux/Windows, swap the `sips` line for `magick docs/architecture.png docs/architecture.jpg` or any PNG→JPEG converter.)

After regenerating, **open `docs/architecture.jpg` and actually read the node
text** (don't just check the file exists) — visually diff it against the
`architecture.mmd` source (or GitHub's rendered preview of the `.mmd`) to
confirm they match before committing both files together.
