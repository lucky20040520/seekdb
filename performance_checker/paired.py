"""Three interleaved rounds with an independent scalar oracle and fixed baseline."""
import argparse
import hashlib
import heapq
import json
from pathlib import Path
import uuid

from .checker import Container, baseline_medians, calculate, docker, save, workload_phase, LIMITATIONS
from .workload import make_workload, _distance


def prepare_truth(workload, cache):
    digest = hashlib.sha256(json.dumps(workload, separators=(",", ":")).encode()).hexdigest()
    file = Path(cache) / (digest + ".json")
    if file.exists():
        value = json.loads(file.read_text())
        if value["workload_sha256"] != digest or value["oracle"] != "stdlib-math.fsum-v1":
            raise ValueError("invalid oracle cache")
        workload["truth"] = value["truth"]
        return
    truth = []
    for i, request in enumerate(workload["requests"], 1):
        top = heapq.nsmallest(10, ((_distance(row[2], request["query"]), row[0])
              for row in workload["records"] if request["tag"] is None or row[1] == request["tag"]))
        truth.append([[rid, distance] for distance, rid in top])
        if i % 100 == 0:
            print("Independent scalar oracle", i, "of", len(workload["requests"]), flush=True)
    file.parent.mkdir(parents=True, exist_ok=True)
    save(file, {"oracle": "stdlib-math.fsum-v1", "workload_sha256": digest, "truth": truth})
    workload["truth"] = truth


def warm_image(image, args):
    volume = "perfcheck-data-" + uuid.uuid4().hex
    docker("volume", "create", volume)
    container = Container(image, volume, "warm-image", args)
    try:
        container.start()
        container.request("CREATE TABLE warm(id BIGINT PRIMARY KEY,v VECTOR(2));")
        container.request("INSERT INTO warm VALUES (1,'[1,2]'),(2,'[3,4]');")
        container.request("SELECT id,L2_DISTANCE(v,'[1,2]') AS d FROM warm ORDER BY d LIMIT 1;")
        container.close()
    finally:
        container.cleanup()
        docker("volume", "rm", volume)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--images", nargs="+", required=True, help="label=image pairs")
    p.add_argument("--baseline", help="fixed three-round Python result")
    p.add_argument("--freeze-baseline-label", help="freeze one explicitly selected variant's three rounds as the baseline for this workload profile")
    p.add_argument("--output", required=True)
    p.add_argument("--oracle-cache", required=True)
    p.add_argument("--io-device")
    p.add_argument("--allow-missing-resources", action="store_true", help="explicit correctness/time-only CI diagnostic; never produces E")
    p.add_argument("--create-index", action="store_true")
    p.add_argument("--rows", type=int, default=10000)
    p.add_argument("--dimensions", type=int, default=128)
    p.add_argument("--queries", type=int, default=1000)
    p.add_argument("--rounds", type=int, default=3)
    args = p.parse_args()
    if args.rows < 10 or args.dimensions <= 0 or args.queries < 2 or args.queries % 2 or args.rounds <= 0:
        p.error("rows >= 10, positive dimensions/rounds, and even queries >= 2 are required")
    if not args.io_device and not args.allow_missing_resources:
        p.error("--io-device is required for six-metric measurements")
    args.k, args.mode = 10, "exact"
    args.cpus, args.memory, args.pids = "2", "2g", 128
    args.startup_timeout, args.request_timeout, args.max_response_bytes = 120, 60, 16777216
    args.cgroup_root, args.volume_root = "/host-cgroup", "/host-volumes"
    if args.allow_missing_resources:
        args.cgroup_root = None
    images = dict(item.split("=", 1) for item in args.images)
    if len(images) != len(args.images):
        p.error("image labels must be unique")
    if args.freeze_baseline_label and (args.baseline or args.allow_missing_resources or args.rounds != 3 or args.freeze_baseline_label not in images):
        p.error("freezing a baseline requires its image label, three rounds, and no existing baseline")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    config = {k: getattr(args, k) for k in ("rows", "dimensions", "queries", "k", "mode", "create_index", "rounds", "cpus", "memory", "pids", "io_device")}
    if args.baseline:
        baseline_config = json.loads(Path(args.baseline).read_text())["configuration"]
        if any(baseline_config.get(key) != value for key, value in config.items()):
            raise ValueError("baseline workload, index policy or resource limits differ")
    result = {"scope": "E_local against frozen Python; not official score", "configuration": config,
              "limitations": LIMITATIONS, "status": "running", "variants": {}, "order": []}
    for name, image in images.items():
        info = json.loads(docker("image", "inspect", image).stdout)[0]
        if any(x.startswith("MINIVECTOR_PROFILE=") and x != "MINIVECTOR_PROFILE=0" for x in info["Config"].get("Env", [])):
            raise ValueError("profiling must be disabled in measured images")
        if any(x.startswith("LD_PRELOAD=") and x != "LD_PRELOAD=" for x in info["Config"].get("Env", [])):
            raise ValueError("test instrumentation must be absent from measured images")
        result["variants"][name] = {"image": image, "image_id": info["Id"], "configuration": config, "rounds": []}
        warm_image(image, args)
    report = output / "result.json"
    save(report, result)
    try:
        for number in range(args.rounds):
            workload = make_workload(args.rows, args.dimensions, args.queries, args.k, 19017 + number, 29017 + number)
            prepare_truth(workload, args.oracle_cache)
            names = list(images)
            if number % 2:
                names.reverse()
            result["order"].append(names)
            for name in names:
                print("Round", number + 1, name, flush=True)
                write = workload_phase(images[name], workload, args, "write")
                query = workload_phase(images[name], workload, args, "query")
                checks = {"write_correct": write["persisted"], "query_correct": query["persisted"],
                          "resources_ok": all(not part["raw_resources"]["errors"] for part in (write, query)),
                          "recall_unfiltered": query["recall"]["unfiltered"], "recall_filtered": query["recall"]["filtered"]}
                row = {"round": number + 1, "write": write, "query": query, "checks": checks}
                value = result["variants"][name]
                value["rounds"].append(row)
                save(output / (name + ".json"), value)
                save(report, result)
                print(json.dumps({"variant": name, "round": number + 1,
                      **{part: {k: row[part][k] for k in ("time_ms", "io_bytes", "memory_bytes")} for part in ("write", "query")}}), flush=True)
        if args.freeze_baseline_label:
            args.baseline = str(output / (args.freeze_baseline_label + ".json"))
            result["fixed_baseline_label"] = args.freeze_baseline_label
        for name, value in result["variants"].items():
            if args.rounds == 3:
                value["efficiency"] = calculate(args.baseline, value["rounds"])
                if all(part[metric] is not None for row in value["rounds"] for part in (row["write"], row["query"]) for metric in ("io_bytes", "memory_bytes")):
                    value["medians"] = baseline_medians(value)
            save(output / (name + ".json"), value)
        result["status"] = "completed"
        save(report, result)
    except BaseException as exc:
        result.update(status="failed", error=str(exc))
        save(report, result)
        raise


if __name__ == "__main__":
    main()
