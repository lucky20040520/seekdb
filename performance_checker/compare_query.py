"""Paired exact-query experiment with full and blocked distance screening.

Uses the existing driver unchanged. Profiling is off; each variant has its own
volume and cgroup. All timed query answers are checked against an independent
Python math.fsum scan after measurement. No full competition score is produced.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

from .checker import docker, save
from .compare_cache import measure
from .workload import make_workload, verify_answer


def summarize(rounds, variants):
    summary = {}
    for name in variants:
        metrics = {}
        for field in ("time_ms", "io_bytes", "memory_bytes", "preparation_ms"):
            values = [r["variants"][name][field] for r in rounds]
            metrics[field] = statistics.median(values) if all(v is not None for v in values) else None
        metrics["groups"] = {group: {"time_ms": statistics.median(r["variants"][name]["groups"][group]["time_ms"] for r in rounds),
                                     "queries": rounds[0]["variants"][name]["groups"][group]["count"]}
                            for group in ("unfiltered", "filtered")}
        summary[name] = metrics
    for name in variants:
        if name == "before":
            continue
        summary[name]["reduction_percent_vs_before"] = {
            field: 100 * (1 - summary[name][field] / summary["before"][field])
            if summary[name][field] is not None and summary["before"][field] else None
            for field in ("time_ms", "io_bytes", "memory_bytes")}
        summary[name]["group_reduction_percent_vs_before"] = {
            group: 100 * (1 - summary[name]["groups"][group]["time_ms"] / summary["before"]["groups"][group]["time_ms"])
            for group in ("unfiltered", "filtered")}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--io-device", required=True)
    parser.add_argument("--blocks", type=int, nargs="+", default=[0, 32], choices=[0, 32, 64, 128])
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimensions", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.set_defaults(cpus="2", memory="2g", pids=128, startup_timeout=120,
                        request_timeout=60, max_response_bytes=16777216)
    args = parser.parse_args()
    if args.rows < 10 or args.dimensions <= 0 or args.queries < 2 or args.queries % 2 or args.k <= 0:
        parser.error("rows >= 10, positive dimensions/K and even queries >= 2 required")
    if len(set(args.blocks)) != len(args.blocks):
        parser.error("block sizes must not repeat")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    path = output / "result.json"
    variants = {"before": (args.before, None)}
    variants.update({"full" if b == 0 else "block%d" % b: (args.after, b) for b in args.blocks})
    root = Path(__file__).resolve().parents[1]
    sources = ["cpp/main.cpp", "cpp/minivector.hpp", "cpp/query.cpp", "cpp/storage.cpp", "cpp/sql.cpp",
               "cpp/profile.hpp", "cpp/distance_screen.hpp", "Dockerfile", "performance_checker/compare_query.py",
               "performance_checker/compare_cache.py", "performance_checker/checker.py",
               "performance_checker/workload.py", "performance_checker/transport.py"]
    report = {"status": "running", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Local exact query comparison, not an official score; profiling disabled",
              "configuration": {**vars(args), "rounds": 3, "data_seed": 19017, "query_seed": 29017},
              "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
              "limitations": ["Resource scope includes load and warmup; query timing excludes both.",
                              "Single unfiltered warmup; equality-list creation is timed.",
                              "Host caches are not explicitly cleared; hardware cache misses/DRAM bytes are not measured.",
                              "No separate write-stage measurement or full efficiency score.",
                              "Device verification does not prove exclusive writeback attribution."],
              "images": {}, "rounds": []}
    save(path, report)
    try:
        for name, (image, block) in variants.items():
            report["images"][name] = {"tag": image, "screen_block": block,
                                      "id": json.loads(docker("image", "inspect", image, "--format", "{{json .Id}}").stdout)}
        names = list(variants)
        for number in range(3):
            workload = make_workload(args.rows, args.dimensions, args.queries, args.k, 19017 + number, 29017 + number)
            order = names[number % len(names):] + names[:number % len(names)]
            row = {"number": number + 1, "measurement_order": order, "variants": {}}
            report["rounds"].append(row)
            answers = {}
            for name in order:
                image, args.screen_block = variants[name]
                print("Round", number + 1, name, "measuring", flush=True)
                row["variants"][name], answers[name] = measure(image, workload, args)
                save(path, report)
                print("Round", number + 1, name, "query_ms=%.3f" % row["variants"][name]["time_ms"], flush=True)
            if any(answer != answers["before"] for answer in answers.values()):
                raise RuntimeError("exact query responses differ across variants")
            row["identical_answers"] = True
            recall = {"unfiltered": [], "filtered": []}
            for index, (request, answer) in enumerate(zip(workload["requests"], answers["before"]), 1):
                value = verify_answer(answer, workload["records"], request["query"], request["tag"], args.k)
                recall["unfiltered" if request["tag"] is None else "filtered"].append(value)
                if index % 100 == 0:
                    print("Round", number + 1, "independent exact truth", index, "/", args.queries, flush=True)
            row["recall"] = {group: statistics.mean(values) for group, values in recall.items()}
            if any(value != 1.0 for value in row["recall"].values()):
                raise RuntimeError("exact query recall must equal 1.0")
            save(path, report)
        report["medians"] = summarize(report["rounds"], variants)
        report["status"] = "completed"
        save(path, report)
        print(json.dumps(report["medians"], indent=2), flush=True)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        save(path, report)
        raise


if __name__ == "__main__":
    main()
