#!/usr/bin/env python3
"""Clone every GitHub repo listed in repo-seeds/registry.json, slowly.

registry.json is the single source of truth for which repos feed the
pipeline -- see registry.py for how entries get added/curated (seed, search,
or manual, each with its own provenance detail).
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import registry as registry_module
import skills_map
from registry import mark_sync_failure, repo_pairs, set_stars

DEST_DIR = Path(__file__).parent / "repos"
ENV_FILE = Path(__file__).parent / ".env"
STATE_FILE = Path(__file__).parent / ".clone_state.json"

MIN_DELAY_SECONDS = 5  # floor pause between clones even when rate limit is healthy
RECLONE_COOLDOWN_SECONDS = 30 * 24 * 60 * 60  # don't re-clone a repo within a month
RATE_LIMIT_SAFETY_MARGIN = 5  # stop and wait when this few requests remain
# Extra flat pause after every successful clone, on top of the rate-limit
# pacing above -- stay conservative with GitHub even when the API says we
# have quota to spare. Skipped for skips/errors since those don't hit the
# clone endpoint.
POST_CLONE_DELAY_SECONDS = 1

REPO_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def load_github_pat():
    """Read GITHUB_PAT from .env (without requiring python-dotenv) or the environment."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "GITHUB_PAT":
                return value.strip().strip('"').strip("'")
    return os.environ.get("GITHUB_PAT")


GITHUB_PAT = load_github_pat()


def _basic_auth_header():
    token = f"x-access-token:{GITHUB_PAT}".encode()
    return base64.b64encode(token).decode()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def recently_cloned(state, owner, repo):
    ts = state.get(f"{owner}/{repo}")
    if ts is None:
        return False
    return (time.time() - ts) < RECLONE_COOLDOWN_SECONDS


def check_rate_limit():
    """Query GitHub's /rate_limit endpoint and pause if we're close to exhausting it.

    Returns the pacing delay (seconds) to use before the next clone, computed
    from the remaining quota and time left in the current window, so a full
    run naturally spreads itself out under whatever GitHub currently allows
    (5000/hr authenticated, 60/hr unauthenticated).
    """
    req = urllib.request.Request("https://api.github.com/rate_limit")
    req.add_header("Accept", "application/vnd.github+json")
    if GITHUB_PAT:
        req.add_header("Authorization", f"Bearer {GITHUB_PAT}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[warn] could not check rate limit ({e}); using default delay")
        return MIN_DELAY_SECONDS

    core = data["resources"]["core"]
    remaining = core["remaining"]
    reset_at = core["reset"]
    now = time.time()
    seconds_to_reset = max(reset_at - now, 1)

    if remaining <= RATE_LIMIT_SAFETY_MARGIN:
        wait = seconds_to_reset + 5
        print(f"[rate-limit] only {remaining} requests left, resets in {seconds_to_reset:.0f}s -- sleeping {wait:.0f}s")
        time.sleep(wait)
        return MIN_DELAY_SECONDS

    # Spread remaining clones evenly across the time left in this window,
    # but never go faster than MIN_DELAY_SECONDS.
    pace = seconds_to_reset / remaining
    return max(MIN_DELAY_SECONDS, pace)


def get_star_count(owner, repo):
    """Query GitHub's repo API for stargazers_count. Returns None on failure."""
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}")
    req.add_header("Accept", "application/vnd.github+json")
    if GITHUB_PAT:
        req.add_header("Authorization", f"Bearer {GITHUB_PAT}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[warn] could not fetch star count for {owner}/{repo}: {e}")
        return None
    return data.get("stargazers_count")


def clone_repo(owner, repo, state):
    """Returns "skipped", "cloned", or "error" -- callers use this to decide
    whether the rate-limit pacing delay is actually needed."""
    dest = DEST_DIR / owner / repo

    if recently_cloned(state, owner, repo):
        print(f"[skip] {owner}/{repo} cloned within the last 30 days")
        return "skipped"

    if dest.exists():
        print(f"[skip] {owner}/{repo} already exists at {dest}")
        state[f"{owner}/{repo}"] = time.time()
        return "skipped"

    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{repo}.git"
    stars = get_star_count(owner, repo)
    stars_label = f"{stars} stars" if stars is not None else "stars unknown"
    print(f"[clone] {url} ({stars_label}) -> {dest}")
    if stars is not None:
        set_stars(owner, repo, stars)

    cmd = ["git", "clone", "--depth", "1"]
    if GITHUB_PAT:
        # Pass the token via an extraheader instead of embedding it in the URL,
        # so it never ends up in the cloned repo's .git/config or in `ps` output.
        cmd += ["-c", f"http.extraheader=AUTHORIZATION: basic {_basic_auth_header()}"]
    cmd += [url, str(dest)]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error = result.stderr.strip()
        print(f"[error] {owner}/{repo}: {error}")
        try:
            mark_sync_failure(owner, repo, error)
        except ValueError:
            pass  # repo not in registry (e.g. one-off URL clone) -- nothing to record
        return "error"
    state[f"{owner}/{repo}"] = time.time()
    return "cloned"


def parse_single_url(arg):
    m = REPO_URL_RE.match(arg.strip())
    if not m:
        return None
    owner, repo = m.groups()
    return owner, repo.rstrip(".").removesuffix(".git")


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Clone repos listed in repo-seeds/registry.json, slowly.",
    )
    parser.add_argument(
        "url_or_limit",
        nargs="?",
        help="Either a one-off GitHub repo URL to clone directly, or a numeric limit "
        "(clone only the first N repos in registry order). Omit to clone everything.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many repos at the start of registry order before applying the "
        "limit, e.g. --offset 100 url_or_limit=100 clones repos 100-199. Used to work "
        "through the registry in fixed-size batches under a disk-space budget.",
    )
    parser.add_argument(
        "--source",
        metavar="TYPE",
        help="Only clone repos that have a descriptor of this source type in the registry "
        f"(one of {sorted(registry_module.VALID_SOURCES)}), e.g. --source skills.sh to "
        "clone only repos surfaced by the leaderboard.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Shorthand for a small limit (2 repos) to sanity-check the end-to-end pipeline "
        "without a long clone run. Combine with --source to debug a specific channel, e.g. "
        "--source skills.sh --debug. Overridden by an explicit url_or_limit if both are given.",
    )
    parser.add_argument(
        "--only-unsynced",
        action="store_true",
        help="Filter to registry entries whose last_synced isn't from today (never synced, "
        "or stale), per registry.unsynced_today(). Skips already-synced-today repos by "
        "content instead of registry position -- unlike --offset, this works even when "
        "synced and unsynced repos are interleaved in registry order.",
    )
    return parser


def main():
    if not GITHUB_PAT:
        print(f"[warn] no GITHUB_PAT found in {ENV_FILE} or environment; cloning unauthenticated (lower rate limit)")

    state = load_state()

    args = _build_arg_parser().parse_args()

    if args.url_or_limit and not args.url_or_limit.isdigit():
        # one-off link mode: python3 clone_repos.py https://github.com/owner/repo
        single = parse_single_url(args.url_or_limit)
        if not single:
            print(f"[error] not a valid GitHub repo URL: {args.url_or_limit}")
            sys.exit(1)
        result = clone_repo(*single, state)
        save_state(state)
        if result != "error":
            skills_map.update_repo(*single)
        return

    if args.only_unsynced:
        unsynced = registry_module.unsynced_today()
        unsynced_keys = {(r["owner"], r["repo"]) for r in unsynced}
        repos = [pair for pair in repo_pairs(source=args.source) if pair in unsynced_keys]
        source_label = f" with source={args.source!r}" if args.source else ""
        print(f"Found {len(repos)} unsynced-today repos in repo-seeds/registry.json{source_label}")
    else:
        repos = repo_pairs(source=args.source)
        source_label = f" with source={args.source!r}" if args.source else ""
        print(f"Found {len(repos)} repos in repo-seeds/registry.json{source_label}")

    if args.offset:
        repos = repos[args.offset:]
        print(f"[offset] skipping first {args.offset} repos -- {len(repos)} remaining")

    limit = None
    if args.url_or_limit:
        limit = int(args.url_or_limit)
    elif args.debug:
        limit = 2
        print("[debug] limiting to first 2 repos")
    if limit is not None:
        repos = repos[:limit]

    counts = {"cloned": 0, "skipped": 0, "error": 0}
    for i, (owner, repo) in enumerate(repos):
        result = clone_repo(owner, repo, state)
        counts[result] += 1
        save_state(state)
        # Refresh repo-seeds/skills.json every run regardless of clone
        # outcome (cloned or skipped-as-already-present) -- registry.json's
        # sources for this repo can change between runs even when its
        # content on disk doesn't.
        if result != "error":
            skills_map.update_repo(owner, repo)
        # Only pace against the rate limit when a clone actually happened --
        # skips (already-cloned repos) don't touch the GitHub API.
        if result == "cloned" and i < len(repos) - 1:
            time.sleep(POST_CLONE_DELAY_SECONDS)
            delay = check_rate_limit()
            time.sleep(delay)

    print(
        f"Done: {counts['cloned']} cloned, {counts['skipped']} skipped, "
        f"{counts['error']} errors (of {len(repos)} total)"
    )


if __name__ == "__main__":
    main()
