#!/usr/bin/env python3
"""For entries in npm_mcp_candidates.json with no readme from npm (monorepo
packages usually -- npm just doesn't have one on file), backfill it in two
tiers:
  1. GitHub raw -- README.md via raw.githubusercontent.com/<owner>/<repo>/HEAD/...
     (no clone), when repository/homepage points at github.com.
  2. npm tarball -- every npm package has one regardless of repo linkage, so
     for whatever's left, download the .tgz and pull package/README.md out
     of it directly. Slower/heavier, hence tier 2, but always available.

Rate limited like the npm fetch.

Usage:
    python backfill_readmes.py
"""

import io
import json
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_mcp import github_fetcher, parse_github_repo_url
from shared.rate_limit import sleep_if_more

DATA_PATH = Path(__file__).parent / "npm_mcp_candidates.json"
REQUEST_INTERVAL_SECONDS = 2.0  # GitHub raw / npm both have generous limits; still pace it.


def repo_url_from_entry(entry: dict) -> str | None:
    repo = entry.get("repository")
    if isinstance(repo, dict) and repo.get("url"):
        return repo["url"]
    if isinstance(repo, str) and repo:
        return repo
    # Some packages only put the GitHub link in homepage, not repository.
    homepage = entry.get("homepage")
    if homepage and "github.com" in homepage:
        return homepage
    return None


def readme_from_tarball(package_name: str) -> str | None:
    """Fetch a package's dist tarball and extract package/README.md. Works
    for any published package regardless of whether it links a repo."""
    encoded = urllib.parse.quote(package_name, safe="")
    with urllib.request.urlopen(f"https://registry.npmjs.org/{encoded}") as resp:
        doc = json.load(resp)
    latest = doc.get("dist-tags", {}).get("latest")
    tarball_url = ((doc.get("versions") or {}).get(latest, {}).get("dist") or {}).get("tarball")
    if not tarball_url:
        return None

    with urllib.request.urlopen(tarball_url) as resp:
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for name in tf.getnames():
            if name.lower() == "package/readme.md":
                member = tf.extractfile(name)
                return member.read().decode(errors="ignore") if member else None
    return None


def backfill(entries: list[dict]) -> tuple[list[dict], int, int]:
    missing = [e for e in entries if not e.get("readme")]
    print(f"{len(missing)} entries missing a readme; backfilling from GitHub")

    filled = skipped = 0
    for i, entry in enumerate(missing, start=1):
        readme = None
        source = None

        url = repo_url_from_entry(entry)
        owner_repo = None
        if url:
            try:
                owner_repo = parse_github_repo_url(url)
            except ValueError:
                owner_repo = None

        if owner_repo:
            owner, repo = owner_repo
            try:
                readme = github_fetcher(owner, repo)("README.md")
                if readme:
                    source = f"github:{owner}/{repo}"
            except urllib.error.HTTPError:
                readme = None

        if not readme:
            # Tier 2: no usable GitHub link, or GitHub had no root README.md
            # (e.g. it's nested in a monorepo subpackage) -- pull it out of
            # the npm tarball itself, which always exists.
            try:
                readme = readme_from_tarball(entry["name"])
                if readme:
                    source = "npm-tarball"
            except (urllib.error.HTTPError, tarfile.TarError, EOFError):
                readme = None

        if readme:
            entry["readme"] = readme
            entry["readme_filename"] = "README.md"
            entry["readme_source"] = source
            filled += 1
            print(f"[{i}/{len(missing)}] {entry['name']}: filled from {source} ({len(readme)} chars)")
        else:
            print(f"[{i}/{len(missing)}] {entry['name']}: no README found via any tier")
            skipped += 1

        sleep_if_more(i, len(missing), REQUEST_INTERVAL_SECONDS)

    return entries, filled, skipped


def main():
    entries = json.loads(DATA_PATH.read_text())
    for e in entries:
        e.setdefault("readme_source", "npm" if e.get("readme") else None)

    entries, filled, skipped = backfill(entries)

    DATA_PATH.write_text(json.dumps(entries, indent=2))

    with_readme = sum(1 for e in entries if e.get("readme"))
    print(f"\nfilled {filled}, skipped {skipped} -- {with_readme}/{len(entries)} now have a readme")


if __name__ == "__main__":
    main()
