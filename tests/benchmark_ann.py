"""Reproducible local ANN recall and latency experiment (not a unit test)."""
import argparse
import json
import random
import tempfile
import time
import tracemalloc

from minivectordb.storage import Database
from minivectordb.ann import ProjectionIndex


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--budget", default="auto", help="positive candidate count, or auto")
    parser.add_argument("--bits", type=int, default=ProjectionIndex.BITS)
    parser.add_argument("--dimension", type=int, default=16)
    args = parser.parse_args()
    budget = args.budget if args.budget == "auto" else int(args.budget)
    ProjectionIndex.BITS = args.bits
    rng = random.Random(18037)
    vectors = [[rng.uniform(-1, 1) for _ in range(args.dimension)] for _ in range(args.rows)]
    queries = [[rng.uniform(-1, 1) for _ in range(args.dimension)] for _ in range(args.queries)]
    with tempfile.TemporaryDirectory() as directory:
        db = Database(directory)
        db.create_table("docs", {"id": ["BIGINT", None], "category": ["BIGINT", None], "v": ["VECTOR", args.dimension]}, "id")
        db.insert("docs", ({"id": i, "category": i % 5, "v": v} for i, v in enumerate(vectors)))
        db.create_vector_index("vec", "docs", "v")
        start = time.perf_counter()
        tracemalloc.start()
        db._build_vector_index("vec")
        _, build_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        build_seconds = time.perf_counter() - start
        index = db.vector_runtime["vec"]
        index_bytes = sum(file.tell() for file in index.buckets.values())
        real_get = db.get
        reads = [0]
        def counted_get(table, key):
            reads[0] += 1
            return real_get(table, key)
        db.get = counted_get
        results = {}
        for label, predicate in (("unfiltered", lambda row: True), ("filtered", lambda row: row["category"] == 1)):
            recalls, timings = [], {"exact": [], "ann": []}
            counts = {"exact": [], "ann": []}
            for query_no, query in enumerate(queries):
                outputs = {}
                for mode in (("exact", "ann") if query_no % 2 == 0 else ("ann", "exact")):
                    reads[0] = 0
                    start = time.perf_counter()
                    outputs[mode] = db.search("docs", "v", query, 10, predicate, mode=mode,
                                              candidate_budget=budget if mode == "ann" else None)
                    timings[mode].append(time.perf_counter() - start)
                    counts[mode].append(reads[0])
                truth = {r[1] for r in outputs["exact"]}
                recalls.append(len(truth & {r[1] for r in outputs["ann"]}) / len(truth) if truth else 1.0)
            results[label] = {"mean_recall": sum(recalls) / len(recalls), "min_recall": min(recalls),
                              "exact_ms": 1000 * sum(timings["exact"]) / len(queries),
                              "ann_ms": 1000 * sum(timings["ann"]) / len(queries),
                              "mean_exact_row_reads": sum(counts["exact"]) / len(queries),
                              "mean_ann_row_reads": sum(counts["ann"]) / len(queries),
                              "max_ann_row_reads": max(counts["ann"])}
        print(json.dumps({"rows": args.rows, "dimension": args.dimension, "queries_per_group": args.queries, "budget": budget, "bits": args.bits,
                          "build_seconds": build_seconds, "build_peak_python_bytes": build_peak,
                          "index_bytes": index_bytes, "open_bucket_files": len(index.buckets),
                          "results": results}, indent=2))
        db.close()


if __name__ == "__main__":
    main()
