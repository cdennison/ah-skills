#!/usr/bin/env python3
"""Final download step: fetch a README for every registry row with a
resolvable GitHub repo_url that doesn't have a *fresh* one yet -- deduped by
repo_url, so a server surfaced by all three sources (official registry +
glama + the seed list) only ever costs one README fetch, not three.

"Fresh" means fetched within the last README_COOLDOWN_DAYS (30, mirroring
../clone_repos.py's RECLONE_COOLDOWN_SECONDS for skill repos) -- a row with
an older readme_fetched timestamp is treated as a candidate again, same as
one with no readme at all. A *failed* attempt (every tier came up empty)
gets its own, much shorter FAILURE_RETRY_COOLDOWN_HOURS (24) via a separate
readme_last_attempt timestamp -- without it, a dead/private/spam registry
entry (a large fraction of candidates, empirically) would be re-attempted
from scratch on every supervisor.sh restart, since a failure never earns it
a readme_path for is_readme_fresh to protect. Together these are what make
re-running this script safe and cheap after a crash-and-restart: it only
ever re-does what's actually missing, stale, or not recently tried.

Three tiers, cheapest first, each only attempted if the previous one
failed:

  1. raw.githubusercontent.com/<owner>/<repo>/HEAD/README.md -- one HTTP
     GET, just the file bytes. Fast, but assumes the exact filename
     "README.md"; misses READMEs named readme.md/.rst/.txt or similar.
  2. api.github.com/repos/<owner>/<repo>/readme -- GitHub's own
     readme-resolution endpoint. Slightly heavier (JSON + base64 decode)
     but finds the *actual* root readme regardless of name/case/extension,
     so it recovers tier 1's filename-guess misses. Also a clean
     repo-existence check: a 404 here means GitHub genuinely has no
     recognized readme at the root, not that we guessed the filename wrong.
  3. Shallow clone (`git clone --depth 1`) -- last resort only, per
     instruction: a git clone is heavier and slower than either GET above,
     which is exactly why PROPOSED_PIPELINE.md says to avoid it as a
     default. Used only when tiers 1 and 2 both come back empty (e.g. the
     readme isn't at the repo root at all, or is nested in a path GitHub's
     own endpoint doesn't surface). The clone is deleted immediately after
     extracting whatever README* file it finds -- never left on disk, so
     this can't turn into the disk-bloat problem clone-at-scale causes.

All three tiers hit a GitHub host, so all three are authenticated via
GITHUB_PAT (shared/github_auth.py: env var, then .env.local, then .env) --
raises api.github.com from 60/hr to 5000/hr and authenticates the raw/clone
requests too. Falls back to unauthenticated behavior (just slower) if no
token is configured anywhere.

Rate limited via shared.http.github_limiter() (4000/hr shared across every
GitHub host, plus a 10/s burst guard; same shared instance paces all three
tiers, including the clone, so the cap applies to the whole run regardless
of which tier is doing the work). A 429 anywhere sleeps 70 minutes before
retrying rather than giving up (see shared/http.py). Progress saved to
mcp-repo-seeds/registry.json incrementally. Readmes are written to
mcp-search-raw/readmes/<owner>__<repo>.md; the registry row records
readme_path/readme_source (one of "github-raw", "github-api",
"git-clone-shallow")/readme_fetched.

Usage:
    python download_readmes.py
    python download_readmes.py --limit 50       # testing
    python download_readmes.py --no-clone        # tiers 1+2 only, skip the last-resort clone
    python download_readmes.py --refetch         # re-fetch readmes already on disk
"""

import argparse
import base64
import datetime
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_registry
from shared.github_auth import auth_headers, git_clone_extraheader_args
from shared.http import RATE_LIMIT_SLEEP_SECONDS, USER_AGENT, get_text_or_none, github_limiter

SAVE_EVERY = 250  # save_registry() on a ~90-150MB registry.json costs ~1s;
# at the original 25 this was ~40+ min of pure serialization overhead alone
# across a 50k-80k-candidate run -- 250 trades a bit more lost progress on
# a mid-run crash (supervisor.sh already makes that cheap to recover from)
# for a 10x cut in that overhead.
CLONE_TIMEOUT_SECONDS = 60
CLONE_TMP_DIR = mcp_registry.RAW_DIR / ".tmp_clones"

# Same idea as ../clone_repos.py's RECLONE_COOLDOWN_SECONDS (30 days) for
# skill repos: a readme already on disk isn't re-fetched until it's this
# old, so a crash-and-restart under supervisor.sh (or any other re-run)
# doesn't burn rate-limit budget and time re-downloading thousands of
# readmes that haven't gone stale -- only genuinely missing or
# aged-out ones are treated as candidates.
README_COOLDOWN_DAYS = 30


def is_readme_fresh(row: dict) -> bool:
    if not row.get("readme_path"):
        return False
    fetched = row.get("readme_fetched")
    if not fetched:
        return False
    try:
        fetched_at = datetime.datetime.fromisoformat(fetched)
    except ValueError:
        return False
    return (datetime.datetime.now() - fetched_at) < datetime.timedelta(days=README_COOLDOWN_DAYS)


# Separate, much shorter cooldown for a *failed* attempt (all tiers came up
# empty) -- found via real data mid-run that ~45% of candidates end up in
# this bucket (dead/private/spam registry entries -- see MCP_PIPELINE.md's
# notes on official-registry spam). Without this, every supervisor.sh
# restart would re-attempt every one of those from scratch (is_readme_fresh
# alone only ever protects successes, since a failure never gets a
# readme_path), burning rate-limit budget re-discovering the same dead
# repos are dead. 24h is plenty for a single overnight run -- long enough to
# survive any number of crash-restarts tonight, short enough that a
# routine future re-run still re-checks failures reasonably often.
FAILURE_RETRY_COOLDOWN_HOURS = 24


def recently_attempted(row: dict) -> bool:
    last = row.get("readme_last_attempt")
    if not last:
        return False
    try:
        last_at = datetime.datetime.fromisoformat(last)
    except ValueError:
        return False
    return (datetime.datetime.now() - last_at) < datetime.timedelta(hours=FAILURE_RETRY_COOLDOWN_HOURS)

# GitHub's own readme-resolution preference, roughly -- used to pick among
# multiple README* files found in a shallow clone (tier 3 only; tier 2 lets
# GitHub's own algorithm decide instead of guessing).
README_EXT_PRIORITY = {".md": 0, ".markdown": 1, ".rst": 2, ".txt": 3, "": 4}


def fetch_tier1_raw(owner: str, repo: str, limiter) -> str | None:
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"
    return get_text_or_none(url, limiter)


def fetch_tier2_api(owner: str, repo: str, limiter) -> str | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    limiter.wait()
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json", **auth_headers()}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    content = data.get("content")
    if not content:
        return None
    return base64.b64decode(content).decode(errors="ignore")


def fetch_tier3_shallow_clone(owner: str, repo: str, limiter) -> str | None:
    limiter.wait()
    tmp_dir = CLONE_TMP_DIR / f"{owner}__{repo}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1", "--quiet"]
    cmd += git_clone_extraheader_args()
    cmd += [f"https://github.com/{owner}/{repo}.git", str(tmp_dir)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            # git doesn't surface HTTP 429 as a distinct exit path -- it's a
            # generic non-zero exit with rate-limit wording in stderr. Same
            # backoff discipline as shared.http.get's 429 handling: this is
            # an instruction to back off, not an ordinary clone failure, so
            # sleep well past a typical window rather than treating this
            # repo as just having no readme.
            stderr = result.stderr.lower()
            if "rate limit" in stderr or "429" in stderr:
                print(
                    f"[rate-limit] git clone rate limited for {owner}/{repo} -- "
                    f"sleeping {RATE_LIMIT_SLEEP_SECONDS // 60} min before continuing",
                    file=sys.stderr,
                )
                shutil.rmtree(tmp_dir, ignore_errors=True)
                time.sleep(RATE_LIMIT_SLEEP_SECONDS)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT_SECONDS)
                if result.returncode != 0:
                    return None
            else:
                return None
        candidates = sorted(
            tmp_dir.glob("[Rr][Ee][Aa][Dd][Mm][Ee]*"),
            key=lambda p: README_EXT_PRIORITY.get(p.suffix.lower(), 99),
        )
        if not candidates or not candidates[0].is_file():
            return None
        return candidates[0].read_text(errors="ignore")
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


TIERS = [
    ("github-raw", fetch_tier1_raw),
    ("github-api", fetch_tier2_api),
    ("git-clone-shallow", fetch_tier3_shallow_clone),
]


def fetch_readme(owner: str, repo: str, limiter, use_clone: bool) -> tuple[str | None, str | None]:
    """Try each tier in order, returning (text, tier_name) for the first hit,
    or (None, None) if every tier came back empty."""
    tiers = TIERS if use_clone else TIERS[:2]
    for tier_name, fn in tiers:
        text = fn(owner, repo, limiter)
        if text:
            return text, tier_name
    return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only fetch the first N missing readmes (testing)")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help=(
            f"Re-fetch even if fetched successfully within the last {README_COOLDOWN_DAYS} days, or "
            f"attempted-and-failed within the last {FAILURE_RETRY_COOLDOWN_HOURS}h (ignores both cooldowns)"
        ),
    )
    parser.add_argument("--no-clone", action="store_true", help="Skip tier 3 (shallow clone); only try raw + API tiers")
    args = parser.parse_args()

    from shared.github_auth import GITHUB_PAT

    print(f"GITHUB_PAT: {'configured (5000 req/hr ceiling)' if GITHUB_PAT else 'NOT configured (60 req/hr ceiling on api.github.com)'}")

    registry = mcp_registry.load_registry()
    index = mcp_registry.build_index(registry)

    candidates = []
    for row in registry:
        if not row.get("repo_url"):
            continue
        owner_repo = mcp_registry.parse_github_repo_url(row["repo_url"])
        if not owner_repo:
            continue
        if (is_readme_fresh(row) or recently_attempted(row)) and not args.refetch:
            continue
        candidates.append((row["id"], owner_repo))

    if args.limit:
        candidates = candidates[: args.limit]
    print(f"{len(candidates)} unique repos need a README fetch (clone fallback {'disabled' if args.no_clone else 'enabled, last resort'})")

    mcp_registry.README_DIR.mkdir(parents=True, exist_ok=True)
    limiter = github_limiter()

    ok = failed = 0
    tier_counts: dict[str, int] = {}
    for i, (entry_id, (owner, repo)) in enumerate(candidates, start=1):
        entry = index.get(entry_id)
        if entry is not None:
            entry["readme_last_attempt"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            text, tier_name = fetch_readme(owner, repo, limiter, use_clone=not args.no_clone)
        except Exception as e:
            mcp_registry.record_error(registry, entry_id, "readme", repr(e), index=index)
            print(f"[{i}/{len(candidates)}] {owner}/{repo}: ERROR {e!r}")
            failed += 1
            if i % SAVE_EVERY == 0:
                mcp_registry.save_registry(registry)
            continue

        if text is None:
            mcp_registry.record_error(registry, entry_id, "readme", "no README found via any tier", index=index)
            print(f"[{i}/{len(candidates)}] {owner}/{repo}: no README (all tiers exhausted)")
            failed += 1
        else:
            path = mcp_registry.README_DIR / f"{owner}__{repo}.md"
            path.write_text(text)
            mcp_registry.mark_readme(registry, entry_id, path, tier_name, index=index)
            tier_counts[tier_name] = tier_counts.get(tier_name, 0) + 1
            print(f"[{i}/{len(candidates)}] {owner}/{repo}: ok via {tier_name} ({len(text)} chars)")
            ok += 1

        if i % SAVE_EVERY == 0:
            mcp_registry.save_registry(registry)

    mcp_registry.save_registry(registry)
    shutil.rmtree(CLONE_TMP_DIR, ignore_errors=True)
    print(f"\ndone: {ok} readmes fetched, {failed} missing/failed out of {len(candidates)}")
    for tier_name, count in tier_counts.items():
        print(f"  {tier_name:<20} {count:,}")


if __name__ == "__main__":
    main()
