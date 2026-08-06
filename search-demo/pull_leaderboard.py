#!/usr/bin/env python3
"""Pull the top N skills.sh leaderboard entries and save the raw JSON pages.

MANUAL-ONLY -- never call this from batch_pipeline.py, archived/run_pipeline.sh,
a cron job, or any other automation, and no other script in this repo should
import or invoke it programmatically. It requires a VERCEL_OIDC_TOKEN pulled
by hand from the Vercel project repo (see README.md), which expires and
isn't something a scheduled job can refresh itself. Run it yourself, at the
terminal, only when you deliberately want a fresh leaderboard snapshot.

Everything downstream (add_skillsh_leaderboard.py, clone_repos.py,
extract_search_raw.py, index_qdrant.py) reads the already-saved
leaderboard-raw/combined.json from disk -- none of them re-pull.

Usage:
    python3 pull_leaderboard.py [total] [--out DIR]

    total   how many leaderboard entries to pull (default 1000)
    --out   directory to write raw page JSON into (default ./leaderboard-raw)
"""
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ENV_PATH = Path(__file__).parent / ".env"
API_BASE = "https://skills.sh/api/v1/skills"
PAGE_SIZE = 500
SLEEP_SECONDS = 1


def load_token() -> str:
    import os

    token = os.environ.get("VERCEL_OIDC_TOKEN")
    if token:
        return token
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("VERCEL_OIDC_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    print(
        "No VERCEL_OIDC_TOKEN found in the environment or .env.\n"
        "Pull one from the Vercel project repo -- see update_vercel_token.sh.",
        file=sys.stderr,
    )
    sys.exit(1)


def fetch_page(token: str, page: int, per_page: int) -> dict:
    url = f"{API_BASE}?view=all-time&page={page}&per_page={per_page}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        print(f"Request failed: {e.code} {e.reason}\n{e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    total = int(args[0]) if args else 1000

    out_dir = Path("leaderboard-raw")
    if "--out" in sys.argv:
        out_dir = Path(sys.argv[sys.argv.index("--out") + 1])
    out_dir.mkdir(exist_ok=True)

    token = load_token()

    num_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    all_entries = []
    for page in range(num_pages):
        remaining = total - page * PAGE_SIZE
        per_page = min(PAGE_SIZE, remaining)
        print(f"[fetch] page {page} (per_page={per_page})")
        data = fetch_page(token, page, per_page)

        page_path = out_dir / f"page-{page}.json"
        page_path.write_text(json.dumps(data, indent=2))
        print(f"  saved {page_path}")

        all_entries.extend(data.get("data", []))

        if page < num_pages - 1:
            time.sleep(SLEEP_SECONDS)

    combined_path = out_dir / "combined.json"
    combined_path.write_text(json.dumps(all_entries, indent=2))
    print(f"Done: {len(all_entries)} entries -> {combined_path}")


if __name__ == "__main__":
    main()
