#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["qdrant-client"]
# ///
"""Batched Vettd scan of every skill that carries a `cli_security` verdict,
verifying after each batch that BOTH security scans are present on every
skill folder in it:

  - the OSV `cli_security` payload key (written by build_cli_export.py), and
  - a `vettd_scan_publications` receipt on the folder's SKILL.md point.

Works in units of *skill folder*, not point: a folder is scanned once by
Vettd even if several of its files (SKILL.md + README.md ...) are separate
`cli_security` points, and the receipt lands on the SKILL.md point.

Run `build_cli_export.py` first. Then:

    uv run python cli-security-scan/dual_scan_batched.py [--batch 500] [--limit N]

Per batch: `publish_scans.py <skill dirs>` (subprocess — its own Qdrant
client), then a retrieve-and-check of every point under those folders.
Appends one JSON line per batch to work/dual_scan_progress.jsonl.
Resumable — a folder whose SKILL.md point already has a receipt is skipped.
Aborts if 3 batches in a row come back mostly unscanned.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# line-buffered stdout so `nohup ... > log` shows batch progress live
print = functools.partial(print, flush=True)  # noqa: A001

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from skill_id_util import skill_id_from_path  # noqa: E402

SEARCH_RAW = ROOT / "search-raw"
PROGRESS = HERE / "work" / "dual_scan_progress.jsonl"
BATCH_LOG_DIR = HERE / "work" / "vettd_batch_logs"


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _has_receipt(payload: dict) -> bool:
    return any((loc or {}).get("vettd_scan_publications") for loc in (payload.get("locations") or []))


def _retrieve(client, collection, ids, payload):
    out = []
    for i in range(0, len(ids), 1000):
        out.extend(client.retrieve(collection, ids=ids[i:i + 1000],
                                   with_payload=payload, with_vectors=False))
    return out


def build_dir_index(client, collection: str):
    """One full scroll. Returns:
      dir2pts:      {skill_dir_id: [point_id, ...]}   every point under the folder
      cli_dirs:     {skill_dir_id}                    folders with >=1 cli_security point
      has_skillmd:  {skill_dir_id}                    folders with an indexed SKILL.md point
    """
    dir2pts: dict[str, list[str]] = defaultdict(list)
    cli_dirs: set[str] = set()
    has_skillmd: set[str] = set()
    offset = None
    seen = 0
    while True:
        points, offset = client.scroll(
            collection, with_payload=["path", "cli_security"],
            with_vectors=False, limit=4000, offset=offset,
        )
        for p in points:
            pl = p.payload or {}
            path = pl.get("path")
            if not path:
                continue
            d = skill_id_from_path(path)
            dir2pts[d].append(str(p.id))
            if pl.get("cli_security"):
                cli_dirs.add(d)
            if path.rsplit("/", 1)[-1].casefold() == "skill.md":
                has_skillmd.add(d)
        seen += len(points)
        if offset is None:
            break
    print(f"  scrolled {seen} points -> {len(dir2pts)} folders, {len(cli_dirs)} with cli_security")
    return dir2pts, cli_dirs, has_skillmd


def verify(client, collection: str, dirs: list[str], dir2pts: dict[str, list[str]]) -> dict:
    ids = [pid for d in dirs for pid in dir2pts.get(d, [])]
    got = _retrieve(client, collection, ids, ["cli_security", "locations"])
    by_id = {str(g.id): (g.payload or {}) for g in got}
    both = missing_cli = missing_vettd = 0
    missing = []
    for d in dirs:
        pls = [by_id.get(pid, {}) for pid in dir2pts.get(d, [])]
        has_cli = any(pl.get("cli_security") for pl in pls)
        has_vettd = any(_has_receipt(pl) for pl in pls)
        if has_cli and has_vettd:
            both += 1
        else:
            missing.append(d)
            missing_cli += not has_cli
            missing_vettd += not has_vettd
    return {"checked": len(dirs), "both_ok": both, "missing_cli": missing_cli,
            "missing_vettd": missing_vettd, "missing_dirs": missing[:25]}


def run_publish_scans(dirs: list[Path], env: dict, log_path: Path) -> tuple[int, str]:
    cmd = ["uv", "run", "python", "publish_scans.py", *[str(d) for d in dirs]]
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    log_path.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    return proc.returncode, tail


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", type=int, default=500, help="skill folders per Vettd batch (100-1000)")
    ap.add_argument("--limit", type=int, default=None, help="cap folders processed this run")
    ap.add_argument("--no-resume", action="store_true", help="re-scan folders that already have a receipt")
    args = ap.parse_args(argv)

    from index_qdrant import COLLECTION, get_client
    client = get_client()
    env = {**os.environ, **load_dotenv(ROOT / ".env")}
    BATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("indexing folders from the collection...")
    dir2pts, cli_dirs, has_skillmd = build_dir_index(client, COLLECTION)

    # A folder is Vettd-scannable only if its SKILL.md is its own indexed point
    # -- publish_scans attaches the receipt to that point. The rest (SKILL.md
    # content deduped into another point, or blacklisted) can't get a
    # folder-scoped receipt; list them and move on.
    targets = [d for d in sorted(cli_dirs) if d in has_skillmd]
    unscannable = [d for d in sorted(cli_dirs) if d not in has_skillmd]
    if unscannable:
        (HERE / "work" / "dual_scan_no_skillmd_point.txt").write_text("\n".join(unscannable) + "\n")

    todo = list(targets) if args.no_resume else _filter_todo(client, COLLECTION, targets, dir2pts)
    already = len(targets) - len(todo)
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(cli_dirs)} folders have cli_security | {len(unscannable)} not Vettd-scannable "
          f"(no indexed SKILL.md point -> work/dual_scan_no_skillmd_point.txt) | "
          f"{already} already have a vettd receipt | {len(todo)} to scan this run (batch={args.batch})")
    if not todo:
        print("nothing to do — every CLI-installing skill folder has both scans")
        return 0

    started = dt.datetime.now(dt.timezone.utc).isoformat()
    consecutive_bad = 0
    totals = {"checked": 0, "both_ok": 0, "missing_cli": 0, "missing_vettd": 0}

    for bi in range(0, len(todo), args.batch):
        batch = todo[bi:bi + args.batch]
        n = bi // args.batch + 1
        dirs = [SEARCH_RAW / d for d in batch]

        t0 = time.time()
        log_path = BATCH_LOG_DIR / f"batch_{n:03d}.log"
        rc, tail = run_publish_scans(dirs, env, log_path)
        v = verify(client, COLLECTION, batch, dir2pts)
        elapsed = round(time.time() - t0, 1)

        for k in totals:
            totals[k] += v[k]
        rec = {"batch": n, "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "size": len(batch),
               "publish_scans_rc": rc, "publish_scans_tail": tail, "elapsed_s": elapsed,
               **{k: v[k] for k in ("checked", "both_ok", "missing_cli", "missing_vettd")},
               "batch_log": str(log_path.relative_to(ROOT))}
        if v["missing_dirs"]:
            rec["sample_missing_dirs"] = v["missing_dirs"]
        with PROGRESS.open("a") as f:
            f.write(json.dumps(rec) + "\n")

        pct = 100 * v["both_ok"] // max(v["checked"], 1)
        print(f"[batch {n:>3}/{(len(todo)-1)//args.batch+1}] {len(batch):>4} folders | "
              f"both_ok {v['both_ok']}/{v['checked']} ({pct}%) | "
              f"missing_vettd {v['missing_vettd']} missing_cli {v['missing_cli']} | "
              f"rc={rc} | {elapsed}s | {tail}")

        consecutive_bad = consecutive_bad + 1 if v["missing_vettd"] > 0.6 * len(batch) else 0
        if consecutive_bad >= 3:
            print(f"\nABORT: 3 consecutive batches mostly unscanned — check the Vettd backend / auth "
                  f"({BATCH_LOG_DIR}/batch_*.log)", file=sys.stderr)
            return 2
        time.sleep(1)

    print(f"\nrun {started} -> {dt.datetime.now(dt.timezone.utc).isoformat()}")
    print(f"totals: both_ok {totals['both_ok']}/{totals['checked']} | "
          f"missing_vettd {totals['missing_vettd']} | missing_cli {totals['missing_cli']}")
    print(f"progress: {PROGRESS.relative_to(ROOT)}")
    if totals["missing_vettd"]:
        print("re-run to continue — folders with a receipt are skipped")
    return 0 if not totals["missing_cli"] else 1


def _filter_todo(client, collection, targets, dir2pts):
    """Return targets whose SKILL.md point does NOT yet have a receipt."""
    ids = [pid for d in targets for pid in dir2pts.get(d, [])]
    got = _retrieve(client, collection, ids, ["locations"])
    receipt_by_id = {str(g.id): _has_receipt(g.payload or {}) for g in got}
    return [d for d in targets if not any(receipt_by_id.get(pid) for pid in dir2pts.get(d, []))]


if __name__ == "__main__":
    raise SystemExit(main())
