#!/usr/bin/env python3
import re
import sys
from datetime import date

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ALLOWED_KINDS = {"collection", "individual", "marketplace", "awesome-list", "vendor-skills", "registry"}
URL_RE = re.compile(r"^https://[^\s]+[^/]$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main(path):
    with open(path) as f:
        doc = yaml.safe_load(f) or {}

    if doc.get("version") != 1:
        fail(f"version must be 1, got {doc.get('version')!r}")

    entries = doc.get("entries")
    if not isinstance(entries, list):
        fail("entries must be a list")

    seen_names = set()
    seen_urls = set()
    for i, e in enumerate(entries):
        ctx = f"entries[{i}]"
        for f in ("name", "url", "kind", "scan_paths", "last_verified"):
            if f not in e:
                fail(f"{ctx}: missing field {f}")

        name = e["name"]
        if not NAME_RE.match(name):
            fail(f"{ctx}: name {name!r} must match {NAME_RE.pattern}")
        if name in seen_names:
            fail(f"{ctx}: duplicate name {name!r}")
        seen_names.add(name)

        url = e["url"]
        if not URL_RE.match(url):
            fail(f"{ctx}: url {url!r} must match {URL_RE.pattern}")
        if url in seen_urls:
            fail(f"{ctx}: duplicate url {url!r}")
        seen_urls.add(url)

        if e["kind"] not in ALLOWED_KINDS:
            fail(f"{ctx}: kind {e['kind']!r} not in {sorted(ALLOWED_KINDS)}")

        if not isinstance(e["scan_paths"], list):
            fail(f"{ctx}: scan_paths must be a list")

        requires_crawler = e.get("requires_crawler", False)
        if not requires_crawler and not e["scan_paths"]:
            fail(f"{ctx}: scan_paths is empty but requires_crawler is false")

        try:
            date.fromisoformat(str(e["last_verified"]))
        except ValueError:
            fail(f"{ctx}: last_verified {e['last_verified']!r} is not ISO-8601")

    print(f"OK: {len(entries)} entries valid in {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: validate_registry.py <path-to-registry.yml>")
    main(sys.argv[1])
