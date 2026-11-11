"""Run isolated local Docker diagnostics and calculate the competition indicator.

Running this module never builds an image, installs dependencies, or submits a
result. A workload is started only by the explicit `run` subcommand.
"""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid

from .resources import parse_io_stat, parse_linux_device_number, parse_memory_peak
from .scoring import calculate_report, rank_to_points
from .transport import JsonlSession
from .workload import make_workload, verify_answer, verify_records

LIMITATIONS = [
    "Synthetic dataset and seeds are not verified to be the official workload.",
    "Cold-cache preparation, the dedicated device and cgroup writeback attribution are not independently verified.",
    "Resource counters are read before shutdown; subsequent writes may be omitted.",
    "docker exec collection helpers share the measured cgroup and affect its counters.",
    "The official frozen resource limits, baseline, score and ranking are not established by this tool.",
]


def docker(*args, check=True, timeout=120):
    result = subprocess.run(["docker", *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("docker command failed: " + result.stderr.decode("utf-8", "replace")[-3000:])
    return result


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def baseline_medians(source):
    """Accept fixed baseline metrics or three baseline rounds; no fabricated values."""
    if not isinstance(source, dict):
        raise ValueError("baseline must be a JSON object")
    if "rounds" not in source:
        try:
            return {p: source[p] for p in ("write", "query")}
        except KeyError as exc:
            raise ValueError("baseline must contain write and query metrics") from exc
    rounds = source["rounds"]
    if not isinstance(rounds, list) or len(rounds) != 3:
        raise ValueError("baseline must contain exactly three rounds")
    result = {}
    for phase in ("write", "query"):
        result[phase] = {}
        for metric in ("time_ms", "io_bytes", "memory_bytes"):
            try:
                values = [row[phase][metric] for row in rounds]
            except (KeyError, TypeError) as exc:
                raise ValueError("baseline round missing " + phase + "." + metric) from exc
            if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in values):
                raise ValueError("baseline metrics must be finite non-negative numbers")
            result[phase][metric] = statistics.median(values)
    return result


def calculate(baseline_path, rounds):
    if not baseline_path:
        return {"available": False, "reason": "No baseline supplied; the efficiency indicator cannot be calculated."}
    if len(rounds) != 3:
        return {"available": False, "reason": "Exactly three completed rounds are required."}
    if any(row[phase]["io_bytes"] is None or row[phase]["memory_bytes"] is None
           for row in rounds for phase in ("write", "query")):
        return {"available": False, "reason": "Required cgroup I/O or memory measurement is unavailable."}
    source = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    result = calculate_report(baseline_medians(source), rounds)
    return {"available": True, "scope": "local estimate; not an official performance score or rank",
            "baseline": baseline_medians(source), **result}


class Container:
    def __init__(self, image, volume, label, args):
        self.name = "perfcheck-" + label + "-" + uuid.uuid4().hex[:12]
        self.process = None
        self.session = None
        self.image, self.volume, self.args = image, volume, args

    def start(self):
        cmd = ["docker", "run", "--name", self.name, "-i", "--network=none",
               "--cpus=" + str(self.args.cpus), "--memory=" + self.args.memory,
               "--memory-swap=" + self.args.memory, "--pids-limit=" + str(self.args.pids),
               "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--mount", "type=volume,src=" + self.volume + ",dst=/data", self.image]
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        self.session = JsonlSession(self.process, startup_timeout=self.args.startup_timeout,
                                    request_timeout=self.args.request_timeout,
                                    max_response_bytes=self.args.max_response_bytes)
        start = time.perf_counter()
        self.session.wait_ready()
        return (time.perf_counter() - start) * 1000

    def request(self, sql, mode="exact"):
        result = self.session.execute(sql, mode=mode)
        if result["ok"] is not True:
            raise RuntimeError("SQL failed: " + repr(result.get("error")))
        return result

    def counters(self):
        # sync -f limits the request to the data filesystem, but cannot prove
        # delayed writeback attribution or writes occurring during shutdown.
        sync = docker("exec", self.name, "sync", "-f", "/data", check=False)
        raw = {"sync_exit_code": sync.returncode,
               "sync_stderr": sync.stderr.decode("utf-8", "replace")[-1000:],
               "io_stat": None, "memory_peak": None, "data_device": None, "errors": []}
        stat = docker("exec", self.name, "stat", "-c", "%d", "/data", check=False)
        try:
            if stat.returncode:
                raise ValueError(stat.stderr.decode("utf-8", "replace")[-1000:])
            raw["data_device"] = parse_linux_device_number(stat.stdout.decode("utf-8", "strict"))
        except (ValueError, UnicodeError) as exc:
            raw["errors"].append("data device: " + str(exc))
        for field, file in (("io_stat", "/sys/fs/cgroup/io.stat"),
                            ("memory_peak", "/sys/fs/cgroup/memory.peak")):
            read = docker("exec", self.name, "cat", file, check=False)
            if read.returncode:
                raw["errors"].append(field + ": " + read.stderr.decode("utf-8", "replace")[-1000:])
            else:
                raw[field] = read.stdout.decode("utf-8", "strict")
        io_bytes = memory_bytes = None
        try:
            memory_bytes = parse_memory_peak(raw["memory_peak"])
        except (ValueError, AttributeError, TypeError) as exc:
            raw["errors"].append("memory.peak: " + str(exc))
        try:
            if not self.args.io_device:
                raise ValueError("No explicit data device major:minor supplied")
            if raw["data_device"] != self.args.io_device:
                raise ValueError("Data device does not match supplied device: " + str(raw["data_device"]))
            if sync.returncode:
                raise ValueError("Filesystem sync failed")
            devices = parse_io_stat(raw["io_stat"])
            counters = devices[self.args.io_device]
            # A new container creates a new cgroup; counters are cumulative
            # from startup. They are not declared platform-equivalent.
            io_bytes = counters["rbytes"] + counters["wbytes"]
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            raw["errors"].append("io.stat: " + str(exc))
        return {"io_bytes": io_bytes, "memory_bytes": memory_bytes, "raw": raw}

    def close(self):
        if self.session is not None and self.process is not None and self.process.poll() is None:
            self.session.shutdown()

    def cleanup(self):
        docker("rm", "-f", self.name, check=False)
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=30)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream and not stream.closed:
                    stream.close()


def write_data(container, workload, dimensions):
    container.request("CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(32),v VECTOR(%d));" % dimensions)
    start = time.perf_counter()
    for offset, sql in enumerate(workload["batches"]):
        container.request("BEGIN;")
        result = container.request(sql)
        expected = min(100, len(workload["records"]) - offset * 100)
        if type(result.get("affected_rows")) is not int or result["affected_rows"] != expected:
            raise RuntimeError("wrong INSERT affected_rows")
        container.request("COMMIT;")
    return (time.perf_counter() - start) * 1000


def verify_persistence(image, volume, workload, args):
    # Validation is deliberately performed in another cgroup after measurement.
    container = Container(image, volume, "verify", args)
    try:
        container.start()
        records = workload["records"]
        def rows():
            for start in range(0, len(records), 1000):
                result = container.request("SELECT * FROM docs WHERE id >= %d AND id < %d ORDER BY id;"
                                           % (start, min(start + 1000, len(records))))
                if result.get("columns") != ["id", "tag", "v"] or not isinstance(result.get("rows"), list):
                    raise RuntimeError("invalid record verification response")
                yield from result["rows"]
        verify_records(rows(), records)
        container.close()
        return True
    finally:
        container.cleanup()


def workload_phase(image, workload, args, label):
    volume = "perfcheck-data-" + uuid.uuid4().hex
    docker("volume", "create", volume)
    container = Container(image, volume, label, args)
    try:
        startup_ms = container.start()
        write_ms = write_data(container, workload, args.dimensions)
        answers = []
        preparation_ms = None
        measured_ms = write_ms
        if label == "query":
            prepare = time.perf_counter()
            if args.create_index:
                container.request("CREATE VECTOR INDEX docs_v ON docs(v);")
            container.request(workload["requests"][0]["sql"], args.mode)
            preparation_ms = (time.perf_counter() - prepare) * 1000
            start = time.perf_counter()
            for request in workload["requests"]:
                result = container.request(request["sql"], args.mode)
                if result.get("columns") != ["id", "distance"] or not isinstance(result.get("rows"), list):
                    raise RuntimeError("invalid query columns or rows")
                answers.append(result["rows"])
                if len(answers) % 100 == 0:
                    print("Completed", len(answers), "of", len(workload["requests"]), "queries", flush=True)
            measured_ms = (time.perf_counter() - start) * 1000
        resource = container.counters()
        container.close()
        container.cleanup()
        container = None
        persisted = verify_persistence(image, volume, workload, args)
        groups = {"unfiltered": [], "filtered": []}
        if label == "query":
            for index, (request, answer) in enumerate(zip(workload["requests"], answers), 1):
                # Empty filtered sets are not included in the average.
                if request["tag"] is not None and not any(r[1] == request["tag"] for r in workload["records"]):
                    verify_answer(answer, workload["records"], request["query"], request["tag"], args.k)
                    continue
                recall = verify_answer(answer, workload["records"], request["query"], request["tag"], args.k)
                groups["unfiltered" if request["tag"] is None else "filtered"].append(recall)
                if index % 100 == 0:
                    print("Verified", index, "of", len(answers), "query results", flush=True)
            if not all(groups.values()):
                raise RuntimeError("both recall groups require at least one nonempty query")
        return {"time_ms": measured_ms, "io_bytes": resource["io_bytes"],
                "memory_bytes": resource["memory_bytes"], "startup_ms": startup_ms,
                "load_ms": write_ms, "preparation_ms": preparation_ms,
                "raw_resources": resource["raw"], "persisted": persisted,
                "recall": {k: sum(v) / len(v) for k, v in groups.items() if v},
                "query_counts": {k: len(v) for k, v in groups.items()}}
    finally:
        if container is not None:
            container.cleanup()
        docker("volume", "rm", volume, check=False)


def run(args):
    if args.baseline:
        source = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        baseline = baseline_medians(source)
        passing = {"write_correct": True, "query_correct": True, "resources_ok": True,
                   "recall_unfiltered": 1.0, "recall_filtered": 1.0}
        calculate_report(baseline, [{"write": baseline["write"], "query": baseline["query"],
                                    "checks": passing}] * 3)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    result = {"scope": "local diagnostic; not an official performance score", "limitations": LIMITATIONS,
              "configuration": {"image": args.image, "mode": args.mode, "create_index": args.create_index,
                                "rows": args.rows, "dimensions": args.dimensions, "queries": args.queries,
                                "k": args.k, "rounds": 3, "data_seed": args.data_seed,
                                "query_seed": args.query_seed, "io_device": args.io_device,
                                "cpus": args.cpus, "memory": args.memory, "pids": args.pids},
              "rounds": [], "status": "running"}
    report = output / "result.json"
    save(report, result)
    try:
        for number in range(3):
            print("Preparing round", number + 1, flush=True)
            workload = make_workload(args.rows, args.dimensions, args.queries, args.k,
                                     args.data_seed + number, args.query_seed + number)
            write = workload_phase(args.image, workload, args, "write")
            query = workload_phase(args.image, workload, args, "query")
            row = {"write": write, "query": query,
                   "checks": {"write_correct": write["persisted"], "query_correct": query["persisted"],
                              "resources_ok": write["memory_bytes"] is not None and query["memory_bytes"] is not None
                                  and not write["raw_resources"]["sync_exit_code"] and not query["raw_resources"]["sync_exit_code"],
                              "recall_unfiltered": query["recall"]["unfiltered"],
                              "recall_filtered": query["recall"]["filtered"]}}
            result["rounds"].append(row)
            save(report, result)
            print("Completed round", number + 1, flush=True)
        result["efficiency"] = calculate(args.baseline, result["rounds"])
        result["status"] = "completed"
        save(report, result)
        print("Report:", report)
        return 0
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        save(report, result)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="explicitly run three local Docker benchmark rounds")
    runner.add_argument("--image", required=True)
    runner.add_argument("--output", required=True, help="new output directory")
    runner.add_argument("--baseline", help="JSON file with fixed metrics or three baseline rounds")
    runner.add_argument("--io-device", help="data device major:minor; never inferred")
    runner.add_argument("--mode", choices=("exact", "ann"), default="exact")
    runner.add_argument("--create-index", action="store_true")
    runner.add_argument("--rows", type=int, default=10000)
    runner.add_argument("--dimensions", type=int, default=128)
    runner.add_argument("--queries", type=int, default=1000)
    runner.add_argument("--k", type=int, default=10)
    runner.add_argument("--data-seed", type=int, default=19017)
    runner.add_argument("--query-seed", type=int, default=29017)
    runner.add_argument("--cpus", default="2")
    runner.add_argument("--memory", default="2g")
    runner.add_argument("--pids", type=int, default=128)
    runner.add_argument("--startup-timeout", type=float, default=120)
    runner.add_argument("--request-timeout", type=float, default=60)
    runner.add_argument("--max-response-bytes", type=int, default=16777216)
    score = sub.add_parser("score", help="calculate local indicator from existing JSON; no Docker")
    score.add_argument("--baseline", required=True)
    score.add_argument("--measurements", required=True)
    score.add_argument("--rank", type=int, help="already known competition rank; never inferred")
    args = parser.parse_args(argv)
    if args.command == "score":
        data = json.loads(Path(args.measurements).read_text(encoding="utf-8"))
        result = calculate(args.baseline, data["rounds"])
        if args.rank is not None:
            if not result.get("eligible", False):
                parser.error("points cannot be calculated from ineligible or incomplete measurements")
            result["externally_supplied_rank"] = args.rank
            result["points_for_supplied_rank"] = rank_to_points(args.rank)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.rows < 10 or args.dimensions <= 0 or args.queries < 2 or args.queries % 2 or args.k <= 0:
        parser.error("rows >= 10, positive dimensions/K, and an even query count >= 2 are required")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
