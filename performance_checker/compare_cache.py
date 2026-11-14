"""Paired exact-query cache benchmark; both images use identical seeded data.

Run explicitly with python -m performance_checker.compare_cache --before IMAGE
--after IMAGE --output NEW_DIRECTORY --io-device MAJOR:MINOR.
This measures query performance, not the full write/query competition score.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import time
import uuid

from .checker import Container, LIMITATIONS, docker, save, verify_persistence, write_data
from .workload import make_workload, verify_answer


def measure(image, workload, args):
    volume = "cachecompare-" + uuid.uuid4().hex
    docker("volume", "create", volume)
    container = Container(image, volume, "cache-query", args)
    try:
        startup_ms = container.start()
        load_ms = write_data(container, workload, args.dimensions)
        prepare = time.perf_counter()
        # Same warmup as checker.py: a single unfiltered exact query. The
        # optimized image must build its equality lists inside measured time.
        container.request(workload["requests"][0]["sql"], "exact")
        preparation_ms = (time.perf_counter() - prepare) * 1000
        groups = {name: {"count": 0, "time_ms": 0.0} for name in ("unfiltered", "filtered")}
        answers = []
        start = time.perf_counter()
        for request in workload["requests"]:
            begin = time.perf_counter()
            response = container.request(request["sql"], "exact")
            elapsed = (time.perf_counter() - begin) * 1000
            group = groups["unfiltered" if request["tag"] is None else "filtered"]
            group["count"] += 1
            group["time_ms"] += elapsed
            if response.get("columns") != ["id", "distance"] or not isinstance(response.get("rows"), list):
                raise RuntimeError("invalid query response")
            answers.append(response["rows"])
        time_ms = (time.perf_counter() - start) * 1000
        resource = container.counters()
        container.close()
        container.cleanup()
        container = None
        persisted = verify_persistence(image, volume, workload, args)
        result = {"time_ms": time_ms, "groups": groups, "startup_ms": startup_ms,
                  "load_ms": load_ms, "preparation_ms": preparation_ms,
                  "io_bytes": resource["io_bytes"], "memory_bytes": resource["memory_bytes"],
                  "raw_resources": resource["raw"], "persisted": persisted,
                  "answers_sha256": hashlib.sha256(json.dumps(answers, separators=(",", ":")).encode()).hexdigest()}
        return result, answers
    finally:
        if container is not None:
            container.cleanup()
        docker("volume", "rm", volume, check=False)


def summarize(rounds):
    summary = {}
    for side in ("before", "after"):
        metrics = {}
        for field in ("time_ms", "io_bytes", "memory_bytes", "preparation_ms"):
            values = [row[side][field] for row in rounds]
            metrics[field] = statistics.median(values) if all(v is not None for v in values) else None
        metrics["groups"] = {group: {"time_ms": statistics.median(row[side]["groups"][group]["time_ms"] for row in rounds),
                                      "queries": rounds[0][side]["groups"][group]["count"]}
                             for group in ("unfiltered", "filtered")}
        summary[side] = metrics
    summary["reduction_percent"] = {
        metric: (1 - summary["after"][metric] / summary["before"][metric]) * 100
        if summary["after"][metric] is not None and summary["before"][metric] else None
        for metric in ("time_ms", "io_bytes", "memory_bytes", "preparation_ms")}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--io-device", required=True)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimensions", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.set_defaults(cpus="2", memory="2g", pids=128, startup_timeout=120,
                        request_timeout=60, max_response_bytes=16777216)
    args = parser.parse_args()
    if args.rows < 10 or args.dimensions <= 0 or args.queries < 2 or args.queries % 2 or args.k <= 0:
        parser.error("rows >= 10, positive dimensions/K and an even query count >= 2 required")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "result.json"
    root = Path(__file__).resolve().parents[1]
    paths = ["cpp/minivector.hpp", "cpp/storage.cpp", "cpp/query.cpp", "cpp/sql.cpp", "cpp/main.cpp",
             "Dockerfile", "performance_checker/compare_cache.py", "performance_checker/workload.py",
             "performance_checker/checker.py"]
    report = {"status": "running", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Paired exact query benchmark; local measurements, not an official score",
              "limitations": LIMITATIONS + ["Hardware cache misses and DRAM traffic are not measured.",
                                             "Modeled predicate checks are code/workload-derived counts, not hardware counters.",
                                             "Write performance and the full competition efficiency score are not measured."],
              "configuration": {**vars(args), "mode": "exact", "create_index": False, "rounds": 3,
                                "data_seed": 19017, "query_seed": 29017},
              "source_sha256": {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths},
              "images": {}, "rounds": []}
    save(report_path, report)
    try:
        for side in ("before", "after"):
            report["images"][side] = json.loads(docker("image", "inspect", getattr(args, side),
                                                       "--format", "{{json .Id}}").stdout)
        for number in range(3):
            workload = make_workload(args.rows, args.dimensions, args.queries, args.k,
                                     19017 + number, 29017 + number)
            order = ("before", "after") if number % 2 == 0 else ("after", "before")
            row = {"number": number + 1, "measurement_order": order}
            answers = {}
            report["rounds"].append(row)
            for side in order:
                print("Round", number + 1, side, "measuring", flush=True)
                row[side], answers[side] = measure(getattr(args, side), workload, args)
                save(report_path, report)
                print("Round", number + 1, side, "query_ms=%.3f" % row[side]["time_ms"], flush=True)
            if answers["before"] != answers["after"]:
                raise RuntimeError("before/after exact query answers differ")
            row["identical_answers"] = True
            recall = {"unfiltered": [], "filtered": []}
            for index, (request, answer) in enumerate(zip(workload["requests"], answers["after"]), 1):
                value = verify_answer(answer, workload["records"], request["query"], request["tag"], args.k)
                recall["unfiltered" if request["tag"] is None else "filtered"].append(value)
                if index % 100 == 0:
                    print("Round", number + 1, "verified", index, "queries against independent exact truth", flush=True)
            row["recall"] = {group: statistics.mean(values) for group, values in recall.items()}
            if any(value != 1.0 for value in row["recall"].values()):
                raise RuntimeError("exact query recall is not 1.0")
            filtered = [request for request in workload["requests"] if request["tag"] is not None]
            counts = {tag: sum(record[1] == tag for record in workload["records"])
                      for tag in {request["tag"] for request in filtered}}
            row["modeled_scalar_predicate_checks"] = {
                "before": args.rows * len(filtered),
                "after_including_list_builds": sum(counts[request["tag"]] for request in filtered) + 2 * args.rows * len(counts),
                "assumption": "Each observed tag list fits, is built once in two passes and remains resident."}
            save(report_path, report)
        report["medians"] = summarize(report["rounds"])
        report["status"] = "completed"
        save(report_path, report)
        print(json.dumps(report["medians"], indent=2), flush=True)
        return 0
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        save(report_path, report)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
