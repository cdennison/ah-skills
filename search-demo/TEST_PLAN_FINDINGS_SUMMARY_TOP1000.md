# Test plan: `vettd_scan_findings` summary, top 1000 skills by GitHub stars

Goal: exercise the new high-level findings summary (`publish_scans.py`'s
`_findings_summary`, added alongside `vettd_scan_publications`) across a
real, meaningfully large slice of the actual indexed catalog -- the top
1000 distinct skill directories by GitHub stars -- and confirm it actually
lands in Qdrant, in the right shape, with the right content, not just that
the script exits 0.

**Scope constraint (explicit): only `publish_scans.py` runs.** No
`clone_repos.py`, `extract_search_raw.py`, or `index_qdrant.py` -- this
plan only publishes scans for skill directories already cloned under
`search-raw/` and already indexed in the `agent_skills` Qdrant collection.
Confirmed before writing this plan: all top-1000-by-stars skill
directories are present on disk (0 missing), so this scope constraint
costs nothing in coverage.

Backend: the local dev `vettd-web-1` (`http://localhost:3000`,
`dev@example.com`), not production -- same backend used for every prior
scan-publish test in this repo. Safe to run without a stop-and-check.

---

## 1. Select the target set

Distinct skill **directories** (not location entries -- a directory can
have both `SKILL.md` and `README.md` as separate location entries, which
would otherwise double-count), ranked by the max `stars` seen across any
location under that directory, restricted to directories that exist under
`search-raw/`:

```bash
uv run python3 - <<'EOF'
import os
from qdrant_client import QdrantClient
client = QdrantClient(url="http://localhost:6333", timeout=60)

best_stars_by_dir: dict[str, float] = {}
offset = None
while True:
    points, offset = client.scroll(
        "agent_skills", with_payload=["locations"], with_vectors=False, limit=512, offset=offset
    )
    for p in points:
        for loc in (p.payload or {}).get("locations", []):
            path, stars = loc.get("path"), loc.get("stars")
            if path is None or not isinstance(stars, (int, float)):
                continue
            d = os.path.dirname(path)
            if d not in best_stars_by_dir or stars > best_stars_by_dir[d]:
                best_stars_by_dir[d] = stars
    if offset is None:
        break

ranked = sorted(best_stars_by_dir.items(), key=lambda kv: kv[1], reverse=True)
on_disk = [(d, s) for d, s in ranked if os.path.isdir(os.path.join("search-raw", d))]
top1000 = on_disk[:1000]
with open("/tmp/top1000_skill_dirs.txt", "w") as f:
    for d, _ in top1000:
        f.write("search-raw/" + d + "\n")
print(f"selected {len(top1000)}, star range {top1000[0][1]}-{top1000[-1][1]}")
EOF
```

(Already run once while drafting this plan: 1000/1000 found on disk, star
range 386317-240095.)

## 2. Preflight (same as every prior scan-publish test)

```bash
cd /home/ec2-user/ah-skills/search-demo
set -a && source ./.env && set +a
export SKILLS_QDRANT_URL="http://localhost:6333"
unset SKILLS_QDRANT_DB_PATH
"$VETTD_CLI_BIN" auth status --json   # expect reachable:true, account.email:dev@example.com
curl -s http://localhost:6333/collections/agent_skills | python3 -m json.tool | grep -E '"points_count"|"status"'
```

## 3. Run publish_scans.py against exactly this list -- nothing else

```bash
docker logs vettd-web-1 --tail 0 -f > /tmp/vettd-web-top1000.log 2>&1 &
TAILPID=$!

uv run python3 publish_scans.py $(cat /tmp/top1000_skill_dirs.txt) \
  > /tmp/top1000-publish-summary.log 2> /tmp/top1000-publish-stderr.log
echo "exit:$?"

sleep 3
kill $TAILPID
```

Do **not** run `batch_pipeline.py`, `clone_repos.py`, `extract_search_raw.py`,
or `index_qdrant.py` as part of this test.

## 4. Verify it actually worked -- four checks, not just the exit code

**a. Summary line** (`/tmp/top1000-publish-summary.log`): `attempted=1000`.
`succeeded + skipped` should account for all of `attempted` -- a nonzero
`skipped` count is expected and fine (a directory whose only location is
`README.md`, no `SKILL.md`, is legitimately skipped; see
`_publish_one`'s `skill.md`-only match). `failed` should be `0`; if not,
read every `failed <path>: <message>` line in
`/tmp/top1000-publish-stderr.log` before declaring success.

**b. Backend log**: count `"scan persistence completed"` /
`"status": "ok"` pairs in `/tmp/vettd-web-top1000.log` and compare to
`succeeded` from (a) -- these should match (one persisted scan per
succeeded publish, modulo `"duplicate"` resubmits which still log
persistence).

**c. Qdrant content check** -- for every directory in the target list (not
just a sample, since 1000 is small enough to check exhaustively), confirm:

- `vettd_scan_findings` is present and non-null for every directory that
  `succeeded`.
- `vettd_scan_findings.scan_id` matches the `scan_id` of the *most recent*
  entry in that same location's `vettd_scan_publications`.
- `overall_grade` in `{A, B, C, F, pending, verified}`;
  `trust_level` in `{Trusted, Conditional, Untrusted}`.
- `sum(severity_counts.values()) == finding_count`.
- `len(top_findings) <= 5`, and every entry's `severity != "info"`.
- `categories_flagged` is a subset of
  `{security, structure, best-practices, description, scripts, evals}`.
- **No leakage check**: no `detail` key appears anywhere under
  `vettd_scan_findings` (confirms the free-text/file-snippet field never
  made it into Qdrant, per the approved design).

**d. Distribution sanity** -- report `overall_grade` counts, `trust_level`
counts, and `has_malicious_findings=true` count across all 1000. These are
all high-star, presumably-vetted-by-popularity skills, so a large
malicious-findings count would be a signal to double-check the scanner or
the summary code before trusting the rollout, not something to wave past.

## 5. Appendix: querying Qdrant by stars, security findings, and agent type

Three reusable read-only query patterns, useful beyond this one test run.
**Note on approach**: `agent_skills` has no payload index on nested
`locations[]` fields, so a server-side `scroll_filter` on e.g.
`locations[].path` was observed to time out earlier in this project against
the full 62k+/84k+ point collection (see `TEST_PLAN_CLAUDE_SCANNING.md`'s
own caveats). The reliable approach used throughout this repo's testing is
a client-side scan: page through with `client.scroll(...)` and filter in
Python. Fine at this collection's current size (tens of seconds, not
minutes); if the collection grows much further, a payload index on
`locations[].stars` / `locations[].vettd_scan_findings.categories_flagged`
would be the next step, not attempted here.

**a. Top-N skill directories by stars** (this is exactly section 1's
selection query, generalized):

```python
from qdrant_client import QdrantClient
client = QdrantClient(url="http://localhost:6333", timeout=60)

def top_by_stars(n: int) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            "agent_skills", with_payload=["locations"], with_vectors=False, limit=512, offset=offset
        )
        for p in points:
            for loc in (p.payload or {}).get("locations", []):
                path, stars = loc.get("path"), loc.get("stars")
                if path is None or not isinstance(stars, (int, float)):
                    continue
                d = path  # or os.path.dirname(path) to dedupe by directory
                if d not in best or stars > best[d]:
                    best[d] = stars
        if offset is None:
            break
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:n]
```

**b. Skills with security-category findings** -- checks the new
`vettd_scan_findings` summary. "Has security findings" means
`"security" in categories_flagged` (populated only for non-info-severity
findings, per `_findings_summary`); optionally also filter on
`has_malicious_findings` or a minimum star count:

```python
def with_security_findings(min_stars: float = 0) -> list[dict]:
    results = []
    offset = None
    while True:
        points, offset = client.scroll(
            "agent_skills", with_payload=["locations"], with_vectors=False, limit=512, offset=offset
        )
        for p in points:
            for loc in (p.payload or {}).get("locations", []):
                findings = loc.get("vettd_scan_findings")
                if not findings:
                    continue  # not yet scanned, or scan predates this field
                if "security" not in (findings.get("categories_flagged") or []):
                    continue
                if (loc.get("stars") or 0) < min_stars:
                    continue
                results.append({
                    "path": loc.get("path"),
                    "stars": loc.get("stars"),
                    "overall_grade": findings.get("overall_grade"),
                    "has_malicious_findings": findings.get("has_malicious_findings"),
                    "top_findings": findings.get("top_findings"),
                })
        if offset is None:
            break
    return sorted(results, key=lambda r: r["stars"] or 0, reverse=True)

# e.g. security findings among the top 1000 by stars scanned in this plan:
# with_security_findings(min_stars=240095)
```

(This is the same query used in section 4d's malicious-findings sanity
check, generalized to any severity-bearing security finding rather than
just `has_malicious_findings`.)

**c. Filtering/grouping by `agent_compatibility` -- read this before you rely on it.**

`agent_compatibility` (top-level point payload, e.g. `["openclaw"]` or `[]`)
looks like a clean way to filter "security findings in skills targeting
agent X", but **it's running in a degraded mode on this box's current
data, and 81.1% of points have it empty.** Checked directly against this
collection (62,329 points): `codex: 3541`, `claude-code: 3450`,
`openclaw: 2248`, `generic: 1486`, `copilot: 605`, `cursor: 375`,
`kiro: 223`, `hermes: 221`, `opencode: 160`, `windsurf: 77`, `qwen: 69`,
`cline: 63`, `kimi: 42`, `gemini-cli: 41`, `iflow: 15`,
`factory-droid: 2`, `kilocode: 1` -- everything else (81.1%) is `[]`.

Why: `index_qdrant.py` picks between two classifiers (`agent_target.py`)
depending on whether `repos/<owner>/<repo>/<subpath>` exists on disk at
indexing time:

- `classify_agent_target()` (rich) -- reads plugin manifests
  (`.<agent>-plugin/plugin.json`) and per-skill `agents/<agent>.yaml`
  sidecars. Requires the actual cloned repo under `repos/`.
- `classify_from_metadata()` (fallback, filesystem-free) -- only two
  weaker signals: a path token (`.cursor/`, `.openclaw/`, `.claude/`, ...)
  in the skill's path, or an explicit agent name mentioned in the skill's
  own name/description/owner/repo text. A result of `"unknown"` from
  either classifier is rendered as `agent_compatibility: []`, not a
  literal `"unknown"` string.

`repos/` **does not currently exist on this box** -- it's only populated
transiently during a `batch_pipeline.py` run, between the clone step and
`clean_repos.sh` cleanup. So every point indexed since then (i.e.
effectively this whole collection, as queried today) went through the
weak fallback: a skill only gets tagged if its path happens to contain a
known convention directory, or its name/repo/description happens to
literally mention an agent by name. Most skills, filed under an ordinary
`owner/repo/skills/foo/SKILL.md`-style path with no such mention, are
correctly-but-uninformatively `"unknown"` -> `[]`.

**Practical takeaway**: don't treat an empty `agent_compatibility` as "not
compatible with any agent" -- treat it as "not classified under the
current degraded fallback." A query like "security findings in
`claude-code` skills" will silently undercount by ~4x relative to what a
full re-index with live checkouts would find. If you need real coverage,
that requires re-running `index_qdrant.py` while `repos/` is populated
(i.e. through `batch_pipeline.py`'s clone step, before `clean_repos.sh`
runs) -- out of scope for the scan-publish-only work in this plan.

```python
# safe to use, just keep the above in mind:
def by_agent(agent: str) -> list[str]:
    hits = []
    offset = None
    while True:
        points, offset = client.scroll(
            "agent_skills", with_payload=["path", "agent_compatibility"], with_vectors=False, limit=512, offset=offset
        )
        for p in points:
            if agent in ((p.payload or {}).get("agent_compatibility") or []):
                hits.append(p.payload.get("path"))
        if offset is None:
            break
    return hits
```

## 6. What "success" looks like

- `failed=0` (or every failure explained).
- Every `succeeded` directory has a `vettd_scan_findings` block matching
  the shape and leakage checks in 4c.
- Backend persistence-log count matches `succeeded`.
- Grade/trust/malicious distribution looks like what you'd expect from
  1000 of the most-starred repos on GitHub (mostly clean, mostly A/B
  grade, few if any malicious findings) -- not a red flag in itself, but
  worth a second look if it doesn't.
