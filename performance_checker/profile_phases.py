"""Measure an opt-in database phase profile alongside client-side timings.

Explicitly run: python -m performance_checker.profile_phases --image IMAGE
--output NEW_DIRECTORY [--reference-image IMAGE] [--io-device MAJOR:MINOR].
The default workload matches the local exact-query benchmark. It is diagnostic,
and the instrumentation itself changes timings; it does not compute a score.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import statistics
import time
import uuid

from .checker import Container, docker, save, verify_persistence
from .workload import make_workload, verify_answer

PREFIX = b"MINIVECTOR_PROFILE "
PHASES = {"protocol_parse", "sql_parse", "vector_literal_parse", "stored_row_decode", "filter", "filter_lists",
          "distance_exact", "distance_screen", "heap", "file_sync", "directory_sync", "collect"}


def clock_info(name):
    try:
        clock = time.get_clock_info(name)
    except ValueError:
        return None
    return {"implementation": clock.implementation, "nominal_resolution_seconds": clock.resolution}


def validate_event(event):
    if not isinstance(event, dict) or type(event.get("sequence")) is not int or event["sequence"] < 0:
        raise ValueError("invalid profile sequence")
    if type(event.get("wall_ns")) is not int or event["wall_ns"] < 0:
        raise ValueError("invalid profile wall time")
    phases = event.get("phases")
    if not isinstance(phases, dict) or set(phases) != PHASES:
        raise ValueError("invalid or incomplete profile phases")
    for value in phases.values():
        if (not isinstance(value, dict) or type(value.get("ns")) is not int or value["ns"] < 0
                or type(value.get("calls")) is not int or value["calls"] < 0):
            raise ValueError("invalid profile counter")
    if sum(value["ns"] for value in phases.values()) > event["wall_ns"]:
        raise ValueError("profile phases overlap or exceed request wall time")


def aggregate(records):
    groups = {}
    for record in records:
        group = groups.setdefault(record["group"], {"requests": 0, "roundtrip_ns": 0,
                                                     "client_encode_ns": 0, "client_decode_ns": 0,
                                                     "client_main_cpu_ns": 0, "client_writer_cpu_ns": 0,
                                                     "client_stdout_reader_cpu_ns": 0,
                                                     "engine_wall_ns": 0, "phases": {}})
        group["requests"] += 1
        for key in ("roundtrip_ns", "encode_ns", "decode_ns"):
            target = {"roundtrip_ns": "roundtrip_ns", "encode_ns": "client_encode_ns",
                      "decode_ns": "client_decode_ns"}[key]
            group[target] += record["client"][key]
        for key in ("main_cpu_ns", "writer_cpu_ns", "stdout_reader_cpu_ns"):
            target = "client_" + key
            value = record["client"][key]
            if value is None:
                group[target] = None
            elif group[target] is not None:
                group[target] += value
        event = record.get("engine")
        if event is None:
            group["engine_wall_ns"] = None
            continue
        group["engine_wall_ns"] += event["wall_ns"]
        for name, value in event["phases"].items():
            phase = group["phases"].setdefault(name, {"ns": 0, "calls": 0})
            phase["ns"] += value["ns"]
            phase["calls"] += value["calls"]
    for group in groups.values():
        wall = group["engine_wall_ns"]
        if wall is not None:
            group["engine_other_ns"] = wall - sum(phase["ns"] for phase in group["phases"].values())
            group["roundtrip_minus_engine_ns"] = group["roundtrip_ns"] - wall
    return groups


def run_one(image, profiled, workload, args):
    volume = "phase-profile-" + uuid.uuid4().hex
    docker("volume", "create", volume)
    events = queue.Queue()
    args.profile_engine = profiled
    args.measure_client = True
    args.stderr_line_handler = lambda line: events.put(line) if line.startswith(PREFIX) else None
    container = Container(image, volume, "phase", args)
    records, answers = [], []
    try:
        startup_ms = container.start()

        def request(sql, group):
            sequence = container.session._next_id
            response = container.request(sql)
            records.append({"sequence": sequence, "group": group, "client": dict(container.session.last_timing)})
            return response

        request("CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(32),v VECTOR(%d));" % args.dimensions, "create")
        load_start = time.perf_counter_ns()
        load_cpu_start = time.process_time_ns()
        for offset, sql in enumerate(workload["batches"]):
            request("BEGIN;", "begin")
            result = request(sql, "insert")
            if result.get("affected_rows") != min(100, args.rows - offset * 100):
                raise RuntimeError("unexpected insert count")
            request("COMMIT;", "commit")
        load_ns = time.perf_counter_ns() - load_start
        load_client_cpu_ns = time.process_time_ns() - load_cpu_start or None
        request(workload["requests"][0]["sql"], "warmup")
        query_start = time.perf_counter_ns()
        query_cpu_start = time.process_time_ns()
        for query in workload["requests"]:
            group = "unfiltered" if query["tag"] is None else "filtered"
            response = request(query["sql"], group)
            if response.get("columns") != ["id", "distance"] or not isinstance(response.get("rows"), list):
                raise RuntimeError("invalid query response")
            answers.append(response["rows"])
        query_ns = time.perf_counter_ns() - query_start
        query_client_cpu_ns = time.process_time_ns() - query_cpu_start or None
        resource = container.counters()
        shutdown_sequence = container.session._next_id
        container.session.shutdown()
        records.append({"sequence": shutdown_sequence, "group": "shutdown", "client": dict(container.session.last_timing)})
        startup = None
        if profiled:
            found = {}
            for _ in range(len(records) + 1):
                try:
                    event = json.loads(events.get(timeout=30)[len(PREFIX):].decode("utf-8"))
                    validate_event(event)
                except (queue.Empty, ValueError, UnicodeError) as exc:
                    raise RuntimeError("missing or invalid engine profile event") from exc
                sequence = event.get("sequence")
                if sequence in found:
                    raise RuntimeError("duplicate engine profile sequence")
                found[sequence] = event
            startup = found.pop(0, None)
            if not startup or startup.get("kind") != "startup":
                raise RuntimeError("missing startup profile")
            for record in records:
                event = found.pop(record["sequence"], None)
                if not event or event.get("kind") != "request":
                    raise RuntimeError("missing request profile")
                record["engine"] = event
            if found or not events.empty():
                raise RuntimeError("unexpected profile event")
        elif not events.empty():
            raise RuntimeError("engine profiling was not disabled")
        container.cleanup()
        container = None
        args.profile_engine = False
        args.stderr_line_handler = None
        persisted = verify_persistence(image, volume, workload, args)
        summary = {"startup_ms": startup_ms, "load_ms": load_ns / 1e6, "query_ms": query_ns / 1e6,
                   "load_client_process_cpu_ns": load_client_cpu_ns, "query_client_process_cpu_ns": query_client_cpu_ns,
                   "io_bytes": resource["io_bytes"], "memory_bytes": resource["memory_bytes"],
                   "raw_resources": resource["raw"], "persisted": persisted,
                   "groups": aggregate(records), "startup_engine": startup,
                   "answers_sha256": hashlib.sha256(json.dumps(answers, separators=(",", ":")).encode()).hexdigest()}
        return summary, records, answers
    finally:
        if container is not None:
            container.cleanup()
        docker("volume", "rm", volume, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--reference-image", help="optional image without the profiling instrumentation")
    parser.add_argument("--output", required=True, help="new output directory")
    parser.add_argument("--io-device", help="data device major:minor; not inferred")
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimensions", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--verify-queries", type=int, default=20)
    parser.set_defaults(cpus="2", memory="2g", pids=128, startup_timeout=120, request_timeout=60,
                        max_response_bytes=16777216)
    args = parser.parse_args()
    if args.rows < 10 or args.dimensions <= 0 or args.queries < 2 or args.queries % 2 or args.k <= 0 or not 2 <= args.verify_queries <= args.queries:
        parser.error("rows >= 10, positive dimension/K, even queries >= 2, and 2 <= verify-queries <= queries required")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "result.json"
    source_root = Path(__file__).resolve().parents[1]
    paths = ["cpp/profile.hpp", "cpp/distance_screen.hpp", "cpp/main.cpp", "cpp/sql.cpp", "cpp/storage.cpp", "cpp/query.cpp",
             "cpp/minivector.hpp", "Dockerfile", "performance_checker/profile_phases.py",
             "performance_checker/checker.py", "performance_checker/workload.py", "performance_checker/transport.py"]
    report = {"status": "running", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Local opt-in diagnostic; no official score",
              "configuration": {key: value for key, value in vars(args).items()},
              "client_clocks": {name: clock_info(name) for name in ("perf_counter", "process_time", "thread_time")},
              "source_sha256": {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in paths},
              "limitations": ["Instrumentation perturbs time, particularly hot per-row phases.",
                              "Roundtrip minus engine wall includes IPC, scheduling and measurement-boundary effects; it is not pure driver CPU time.",
                              "Client process CPU covers the benchmark Python process, including collection and bookkeeping, but excludes Docker and database subprocesses; a zero measurement is reported as unavailable, not proof of zero work.",
                              "Short per-request thread CPU times may quantize to zero; they are not proof of no CPU work. Thread counters exclude parts of stdout handling, stderr, Docker and OS work.",
                              "Engine other includes disk reads, output serialization, sorting, setup and instrumentation overhead.",
                              "Resource counters include load and warmup; no dedicated cold-cache or writeback attribution is guaranteed."],
              "runs": {}}
    save(report_path, report)
    try:
        workload = make_workload(args.rows, args.dimensions, args.queries, args.k)
        choices = []
        if args.reference_image:
            choices.append(("reference", args.reference_image, False))
        choices.extend([("disabled", args.image, False), ("enabled", args.image, True)])
        saved_answers = None
        for name, image, enabled in choices:
            print("Measuring", name, flush=True)
            summary, records, answers = run_one(image, enabled, workload, args)
            summary["image"] = image
            summary["image_id"] = json.loads(docker("image", "inspect", image, "--format", "{{json .Id}}").stdout)
            if saved_answers is not None and answers != saved_answers:
                raise RuntimeError("query results differ between runs")
            saved_answers = answers
            with (output / (name + "-requests.jsonl")).open("w", encoding="utf-8", newline="\n") as stream:
                for record in records:
                    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            report["runs"][name] = summary
            save(report_path, report)
            print("Completed", name, "query_ms=%.3f" % summary["query_ms"], flush=True)
        recalls = {"unfiltered": [], "filtered": []}
        # First consecutive queries ensure both groups are independently checked.
        for query, answer in zip(workload["requests"][:args.verify_queries], saved_answers[:args.verify_queries]):
            group = "unfiltered" if query["tag"] is None else "filtered"
            recalls[group].append(verify_answer(answer, workload["records"], query["query"], query["tag"], args.k))
        report["verified_query_counts"] = {group: len(values) for group, values in recalls.items()}
        report["verified_recall"] = {group: statistics.mean(values) for group, values in recalls.items()}
        if any(value != 1.0 for value in report["verified_recall"].values()):
            raise RuntimeError("exact query recall is not 1.0")
        report["status"] = "completed"
        save(report_path, report)
        print("Report:", report_path, flush=True)
        return 0
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        save(report_path, report)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
