#!/usr/bin/env python3
import sys
from datetime import date

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

KEEP = ("name", "url", "ref", "kind", "scan_paths", "requires_crawler")


def main(registry_path):
    with open(registry_path) as f:
        registry = yaml.safe_load(f) or {}

    targets = []
    for e in registry.get("entries", []):
        target = {k: e[k] for k in KEEP if k in e}
        target.setdefault("requires_crawler", False)
        target.setdefault("ref", "main" if target["kind"] in ("collection", "individual", "vendor-skills") else "")
        target.setdefault("scan_paths", [])
        targets.append(target)

    out = {"version": 1, "generated_at": str(date.today()), "targets": targets}
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True, width=120)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: build_scan_manifest.py <registry.yml>")
    main(sys.argv[1])
