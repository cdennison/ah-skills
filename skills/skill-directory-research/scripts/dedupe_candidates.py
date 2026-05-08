#!/usr/bin/env python3
import json
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")


def normalize(url):
    u = url.strip()
    if u.startswith("git@github.com:"):
        u = "https://github.com/" + u[len("git@github.com:"):]
    if u.endswith(".git"):
        u = u[:-4]
    u = re.sub(r"^http://", "https://", u)
    u = u.rstrip("/")
    parts = u.split("/", 3)
    if len(parts) >= 3:
        parts[2] = parts[2].lower()
        u = "/".join(parts)
    return u


def main(registry_path):
    with open(registry_path) as f:
        registry = yaml.safe_load(f) or {}

    known = {normalize(e["url"]) for e in registry.get("entries", [])}
    known_names = {e["name"] for e in registry.get("entries", [])}

    payload = json.load(sys.stdin)
    if isinstance(payload, dict) and "candidates" in payload:
        candidates = payload["candidates"]
    elif isinstance(payload, list):
        candidates = []
        for item in payload:
            if isinstance(item, dict) and "candidates" in item:
                candidates.extend(item["candidates"])
            else:
                candidates.append(item)
    else:
        sys.exit("expected JSON with 'candidates' or a list of scout outputs")

    new = []
    seen = set()
    for c in candidates:
        url = normalize(c["url"])
        if url in known or url in seen:
            continue
        if c.get("name") in known_names:
            continue
        seen.add(url)
        c["url"] = url
        new.append(c)

    json.dump({"new_candidates": new, "skipped": len(candidates) - len(new)}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: dedupe_candidates.py <registry.yml>  < candidates.json")
    main(sys.argv[1])
