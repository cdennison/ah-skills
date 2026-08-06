#!/usr/bin/env python3
"""Download the top-20 all-time skills.sh leaderboard."""
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ENV_PATH = Path(__file__).parent / ".env"
API_URL = "https://skills.sh/api/v1/skills?view=all-time&page=0&per_page=20"
OUT_PATH = Path(__file__).parent / "skills-leaderboard-top20.json"


def load_token() -> str:
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
        "Get one with the Vercel CLI (you said it's already authed), e.g.:\n"
        "  vercel env pull .env\n"
        "or\n"
        '  echo "VERCEL_OIDC_TOKEN=$(vercel env ls --oidc)" >> .env\n'
        "then re-run this script.",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    token = load_token()
    req = Request(API_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        print(f"Request failed: {e.code} {e.reason}\n{e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    OUT_PATH.write_text(json.dumps(data, indent=2))
    print(f"Saved leaderboard to {OUT_PATH}")


if __name__ == "__main__":
    main()
