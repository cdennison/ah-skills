"""Copy qdrant_db_small/ into a payload-only (no-vector) collection and
compare on-disk size + query latency against the vector-backed original.

Read-only against the source DB: scrolls with with_vectors=False and never
opens qdrant_db_small/ for writing.
"""

from __future__ import annotations

import shutil
import statistics
import time
from pathlib import Path

from qdrant_client import QdrantClient, models

ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = ROOT / "qdrant_db_small"
DST_PATH = ROOT / "temp" / "qdrant_db_small_novectors"
COLLECTION = "agent_skills"

N_RUNS = 30
N_WARMUP = 1


def build_novector_copy() -> None:
    if DST_PATH.exists():
        shutil.rmtree(DST_PATH)
    DST_PATH.parent.mkdir(parents=True, exist_ok=True)

    src = QdrantClient(path=str(SRC_PATH))
    dst = QdrantClient(path=str(DST_PATH))

    dst.create_collection(
        COLLECTION,
        vectors_config={},
    )

    offset = None
    total = 0
    while True:
        points, offset = src.scroll(
            COLLECTION,
            with_payload=True,
            with_vectors=False,
            limit=500,
            offset=offset,
        )
        if not points:
            break
        dst.upsert(
            COLLECTION,
            points=[
                models.PointStruct(id=p.id, vector={}, payload=p.payload)
                for p in points
            ],
        )
        total += len(points)
        if offset is None:
            break
    print(f"copied {total} points into {DST_PATH}")
    src.close()
    dst.close()


def du_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def time_calls(fn, n=N_RUNS, warmup=N_WARMUP):
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "min": min(times),
        "median": statistics.median(times),
        "p95": statistics.quantiles(times, n=100)[94] if len(times) >= 20 else max(times),
    }


def run_benchmarks() -> None:
    src = QdrantClient(path=str(SRC_PATH))
    dst = QdrantClient(path=str(DST_PATH))

    star_filter = models.Filter(
        must=[models.FieldCondition(key="stars", range=models.Range(gte=10))]
    )

    results = {}

    def filtered_scroll(client):
        return lambda: client.scroll(
            COLLECTION, scroll_filter=star_filter, with_payload=True, with_vectors=False, limit=50
        )

    def plain_scroll(client):
        return lambda: client.scroll(
            COLLECTION, with_payload=True, with_vectors=False, limit=50
        )

    def count_filtered(client):
        return lambda: client.count(COLLECTION, count_filter=star_filter)

    def count_all(client):
        return lambda: client.count(COLLECTION)

    ops = {
        "filtered_scroll": (filtered_scroll, None),
        "plain_scroll": (plain_scroll, None),
        "count_filtered": (count_filtered, None),
        "count_all": (count_all, None),
    }

    for name, (op_factory, _) in ops.items():
        results[name] = {
            "vector-backed": time_calls(op_factory(src)),
            "no-vector": time_calls(op_factory(dst)),
        }

    src.close()
    dst.close()
    return results


def main() -> None:
    print("Building no-vector copy...")
    build_novector_copy()

    size_src = du_mb(SRC_PATH)
    size_dst = du_mb(DST_PATH)
    pct = (size_dst - size_src) / size_src * 100

    print("\nRunning benchmarks (this rebuilds fresh clients per op)...")
    results = run_benchmarks()

    print("\n=== SIZE ===")
    print(f"{'':20} {'MB':>10}")
    print(f"{'vector-backed':20} {size_src:10.2f}")
    print(f"{'no-vector':20} {size_dst:10.2f}   ({pct:+.1f}%)")

    print("\n=== LATENCY (ms) ===")
    print(f"{'op':18} {'variant':14} {'min':>8} {'median':>8} {'p95':>8}")
    for op, variants in results.items():
        for variant, stats in variants.items():
            print(f"{op:18} {variant:14} {stats['min']:8.3f} {stats['median']:8.3f} {stats['p95']:8.3f}")
        v = variants["vector-backed"]["median"]
        nv = variants["no-vector"]["median"]
        pct_diff = (nv - v) / v * 100 if v else float("nan")
        print(f"  -> median diff: {pct_diff:+.1f}%")
        print()


if __name__ == "__main__":
    main()
