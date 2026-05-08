#!/usr/bin/env python3
import re
import sys
from datetime import date

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

FENCE_RE = re.compile(r"```yaml seed\n(.*?)\n```", re.DOTALL)


def main(seed_path):
    with open(seed_path) as f:
        text = f.read()

    entries = []
    for block in FENCE_RE.findall(text):
        entry = yaml.safe_load(block)
        if not isinstance(entry, dict):
            sys.exit(f"seed block is not a mapping: {block[:80]!r}")
        entry.setdefault("last_verified", str(date.today()))
        entries.append(entry)

    entries.sort(key=lambda e: e["name"])
    out = {"version": 1, "generated_at": str(date.today()), "entries": entries}
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True, width=120)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: seed_registry.py <path-to-seed-directories.md>")
    main(sys.argv[1])
