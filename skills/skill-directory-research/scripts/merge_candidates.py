#!/usr/bin/env python3
import json
import os
import sys
from datetime import date

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

REQUIRED = ("name", "url", "kind", "scan_paths")


def load_yaml(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return yaml.safe_load(f) or default


def dump_yaml(path, doc):
    with open(path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, width=120)


def main(registry_path, verdicts_path):
    registry = load_yaml(registry_path, {"version": 1, "entries": []})
    deferred_path = os.path.join(os.path.dirname(registry_path) or ".", "deferred.yml")
    deferred = load_yaml(deferred_path, {"version": 1, "entries": []})

    with open(verdicts_path) as f:
        verdicts = json.load(f)
    if isinstance(verdicts, dict) and "verdicts" in verdicts:
        verdicts = verdicts["verdicts"]

    by_name = {e["name"]: e for e in registry.get("entries", [])}
    by_url = {e["url"]: e for e in registry.get("entries", [])}
    deferred_by_url = {e["url"]: e for e in deferred.get("entries", [])}

    today = str(date.today())
    added, refreshed, deferred_count, rejected = 0, 0, 0, 0

    for v in verdicts:
        decision = v.get("decision")
        cand = v.get("candidate") or {}
        url = cand.get("url")
        if decision == "approve":
            entry = v.get("suggested_entry") or {}
            for f in REQUIRED:
                if f not in entry:
                    sys.exit(f"approved verdict missing {f}: {entry}")
            entry["last_verified"] = today
            if entry["url"] in by_url:
                existing = by_url[entry["url"]]
                existing.update(entry)
                refreshed += 1
            elif entry["name"] in by_name:
                sys.exit(f"name collision on approval: {entry['name']}")
            else:
                registry.setdefault("entries", []).append(entry)
                by_name[entry["name"]] = entry
                by_url[entry["url"]] = entry
                added += 1
            deferred_by_url.pop(url, None)
        elif decision == "defer":
            if url and url not in deferred_by_url:
                deferred.setdefault("entries", []).append({
                    "url": url,
                    "name": cand.get("name"),
                    "reason": v.get("reason", "deferred"),
                    "first_seen": today,
                })
                deferred_count += 1
        elif decision == "reject":
            rejected += 1
            deferred_by_url.pop(url, None)
        else:
            sys.exit(f"unknown decision {decision!r} for {url}")

    registry["entries"] = sorted(registry.get("entries", []), key=lambda e: e["name"])
    registry["version"] = 1
    registry["generated_at"] = today
    dump_yaml(registry_path, registry)

    deferred["entries"] = list(deferred_by_url.values()) + [
        e for e in deferred.get("entries", []) if e["url"] not in deferred_by_url
    ]
    deferred["version"] = 1
    deferred["generated_at"] = today
    dump_yaml(deferred_path, deferred)

    print(f"added={added} refreshed={refreshed} deferred={deferred_count} rejected={rejected}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: merge_candidates.py <registry.yml> <verdicts.json>")
    main(sys.argv[1], sys.argv[2])
