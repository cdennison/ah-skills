#!/usr/bin/env python3
"""Compare a real plugin install against discover_assets.py's catalogue.

    compare_install.py <assets.json> <installed.txt> [--details <file>] [--surface <id>]

`assets.json`   — output of discover_assets.py for the repo
`installed.txt` — one path per line, RELATIVE TO THE PLUGIN'S INSTALL CACHE ROOT,
                  named "<marketplace>__<plugin>.installed.txt" (from e2e/verify.sh)

The install cache root is the marketplace entry's `source` dir, so installed
paths are joined onto that surface's repo-relative root before comparing.

Exits non-zero if any installed file is neither a catalogued asset nor an
explicit exclusion — those are the misses that matter: a file on the user's
disk that no scanner was told about.
"""
import json
import re
import sys
from pathlib import Path


def main(argv):
    details_file = None
    surface_id = None
    args = []
    it = iter(argv)
    for a in it:
        if a == "--details":
            details_file = next(it)
        elif a == "--surface":
            surface_id = next(it)
        else:
            args.append(a)
    if len(args) < 2:
        print(__doc__)
        return 2

    assets_json, installed_path = args[0], args[1]
    d = json.loads(Path(assets_json).read_text())
    surfaces = {s["id"]: s for s in d["install_surfaces"]}

    # figure out which surface this install corresponds to
    stem = Path(installed_path).name[:-len(".installed.txt")]
    mkt, _, plugin = stem.partition("__")
    if surface_id is None:
        for cand in (f"marketplace:{mkt}/{plugin}", f"plugin_dir:{plugin}:.",
                     f"plugin_dir:.", f"marketplace:{mkt}/{plugin}"):
            if cand in surfaces:
                surface_id = cand
                break
    surf = surfaces.get(surface_id) if surface_id else None
    prefix = ""
    if surf and surf["root"] not in (".", ""):
        prefix = surf["root"].rstrip("/") + "/"

    raw = [l.strip() for l in Path(installed_path).read_text().splitlines() if l.strip()]
    installed = {prefix + f for f in raw}

    catalogued = {a["path"] for a in d["assets"]}
    excluded = {e["path"] for e in d["excluded"]}

    unknown = sorted(f for f in installed if f not in catalogued and f not in excluded)
    in_surface = {a["path"] for a in d["assets"]
                  if surface_id and surface_id in a["surfaces"]}
    ghost = sorted(f for f in in_surface if f not in installed)

    print(f"install id                   : {stem}")
    print(f"mapped to surface            : {surface_id or '(none matched!)'}"
          + (f"   (cache root = repo:/{surf['root']})" if surf and prefix else ""))
    print(f"installed files              : {len(installed)}")
    print(f"catalogued assets for surface : {len(in_surface)}")
    print()

    print(f"=== INSTALLED, NOT ACCOUNTED FOR  ({len(unknown)}) ===")
    print("    (a scanner would never see these — every one is a real miss)")
    for f in unknown:
        print(f"    MISS  {f}")
    print()

    print(f"=== catalogued for this surface but NOT installed  ({len(ghost)}) ===")
    print("    (should be ~0 for a whole-subtree surface; investigate any entry)")
    for f in ghost[:40]:
        print(f"    ghost {f}")
    if len(ghost) > 40:
        print(f"    … and {len(ghost) - 40} more")
    print()

    if details_file and surface_id:
        txt = Path(details_file).read_text()
        print("=== component inventory cross-check (claude plugin details vs this surface) ===")
        kmap = {
            "Skills": ("skill",), "Agents": ("agent",), "Commands": ("command",),
            "Hooks": ("hook_config",), "MCP servers": ("mcp_config", "mcp_server"),
            "LSP servers": ("lsp_config",),
        }
        for kind, kinds in kmap.items():
            m = re.search(rf"{re.escape(kind)} \((\d+)\)", txt)
            if not m:
                continue
            want = int(m.group(1))
            surf_assets = [a for a in d["assets"]
                           if a["kind"] in kinds and surface_id in a["surfaces"]]
            got = len(surf_assets)
            note = ""
            if kind == "Hooks":
                # `claude plugin details` counts hook ENTRIES/events, not files
                entries = 0
                for a in surf_assets:
                    for s in a["signals"]:
                        if s.startswith("hook_entries:"):
                            entries += int(s.split(":")[1])
                note = f"  ({got} hook file(s) wiring {entries} hook entr(y/ies))"
                got = entries or got
            flag = "  OK" if got == want else (
                "  ~ (discover counts per-harness variants)" if got >= want
                else "  ⚠ CHECK — discover found fewer")
            print(f"    {kind:14} details={want:3}   discover[surface]={got}{flag}{note}")
        print()

    if unknown:
        print(f"RESULT: FAIL — {len(unknown)} installed file(s) not accounted for")
        return 1
    print("RESULT: PASS — every installed file is a catalogued asset or an explicit exclusion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
