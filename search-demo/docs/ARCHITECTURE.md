# Architecture diagram

- `architecture.mmd` — the source of truth. A [Mermaid](https://mermaid.js.org/)
  flowchart describing every file/directory in the pipeline and how they
  connect (repo discovery → `repo-seeds/registry.json` → `clone_repos.py` →
  `repos/` → ... → `query.py` / `app/streamlit_app.py`), driven end to end
  by `run_pipeline.sh`. For the recurring curation/maintenance workflow this
  diagram feeds into, see [`../DAILY_JOB.md`](../DAILY_JOB.md).
- `architecture.jpg` — a rendered image of `architecture.mmd`, embedded in
  [`../README.md`](../README.md#architecture) so it also displays for
  viewers whose Markdown renderer doesn't support Mermaid fences.

**The two must be kept in sync by hand.** GitHub renders `.mmd`/Mermaid
fences live, but the `.jpg` does not auto-update — whenever you edit
`architecture.mmd` (new script, new directory, new consumer, etc.),
regenerate `architecture.jpg` in the same change.

## Regenerating architecture.jpg

Pick whichever is available:

**Option A — mermaid.ink (no install required)**

Sends only the diagram text to the public [mermaid.ink](https://mermaid.ink)
rendering service — fine for this non-sensitive architecture diagram.

```bash
cd search-demo
base64_input=$(base64 < docs/architecture.mmd | tr -d '\n')
curl -sL "https://mermaid.ink/img/${base64_input}?bg=white&width=1400" -o docs/architecture.jpg
```

**Option B — mermaid-cli (local, no network dependency)**

Requires [`@mermaid-js/mermaid-cli`](https://github.com/mermaid-js/mermaid-cli)
(`brew install mermaid-cli` or `npm install -g @mermaid-js/mermaid-cli`) and
a headless Chrome (`npx puppeteer browsers install chrome-headless-shell` if
`mmdc` can't find one). `mmdc` only writes `.md`/`.svg`/`.png`/`.pdf`, so
render to PNG and convert:

```bash
cd search-demo
mmdc -i docs/architecture.mmd -o docs/architecture.png -b white -w 1400
sips -s format jpeg docs/architecture.png --out docs/architecture.jpg   # macOS
rm docs/architecture.png
```

(On Linux/Windows, swap the `sips` line for `magick docs/architecture.png docs/architecture.jpg` or any PNG→JPEG converter.)

After regenerating, visually diff `docs/architecture.jpg` against the
`architecture.mmd` source (or GitHub's rendered preview of the `.mmd`) to
confirm they match before committing both files together.
