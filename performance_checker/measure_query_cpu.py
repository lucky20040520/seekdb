"""Run inside the database Linux image to separate process CPU from IPC wait.

Uses the same JSONL session and workload as the Windows-hosted comparison, but
Python and the database share the container. This is a secondary diagnostic,
not an official driver replacement or a cgroup resource/score measurement.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

from .transport import JsonlSession
from .workload import make_workload


def process_ticks(pid):
    text = Path("/proc/%d/stat" % pid).read_text()
    fields = text[text.rfind(")") + 2:].split()
    return int(fields[11]) + int(fields[12])  # utime + stime, fields 14 and 15


def cleanup(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def start(directory, block):
    env = dict(os.environ, MINIVECTOR_PROFILE="0", MINIVECTOR_SCREEN_BLOCK=str(block))
    process = subprocess.Popen(["/usr/local/bin/minivector", "--data-dir", directory],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    session = JsonlSession(process, startup_timeout=120, request_timeout=60, max_response_bytes=16777216)
    try:
        session.wait_ready()
    except BaseException:
        cleanup(process)
        raise
    return process, session


def execute(session, sql):
    result = session.execute(sql)
    if not result.get("ok"):
        raise RuntimeError(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=int, default=0, choices=[0, 32, 64, 128])
    parser.add_argument("--data-dir", default="/data/cpu-check")
    args = parser.parse_args()
    directory = Path(args.data_dir)
    directory.mkdir(parents=True, exist_ok=False)
    workload = make_workload()
    process, session = start(str(directory), args.block)
    try:
        execute(session, "CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(32),v VECTOR(128));")
        for sql in workload["batches"]:
            execute(session, "BEGIN;")
            execute(session, sql)
            execute(session, "COMMIT;")
        session.shutdown()
    finally:
        cleanup(process)
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    rounds = []
    for number in range(3):
        process, session = start(str(directory), args.block)
        try:
            execute(session, workload["requests"][0]["sql"])
            groups = {name: 0.0 for name in ("unfiltered", "filtered")}
            answers = []
            begin_cpu = process_ticks(process.pid)
            begin = time.perf_counter_ns()
            for request in workload["requests"]:
                now = time.perf_counter_ns()
                response = execute(session, request["sql"])
                groups["unfiltered" if request["tag"] is None else "filtered"] += (time.perf_counter_ns() - now) / 1e6
                answers.append(response["rows"])
            wall_ms = (time.perf_counter_ns() - begin) / 1e6
            cpu_ms = (process_ticks(process.pid) - begin_cpu) * 1000 / ticks_per_second
            session.shutdown()
            rounds.append({"round": number + 1, "query_ms": wall_ms, "database_cpu_ms": cpu_ms,
                           "groups_ms": groups,
                           "answers_sha256": hashlib.sha256(json.dumps(answers, separators=(",", ":")).encode()).hexdigest()})
        finally:
            cleanup(process)
    print(json.dumps({"scope": "Linux in-container JSONL client; profiling off, query-only CPU diagnostic",
                      "block": args.block, "rows": 10000, "dimensions": 128, "queries": 1000,
                      "data_seed": 19017, "query_seed": 29017, "cpu_tick_ms": 1000/ticks_per_second,
                      "rounds": rounds,
                      "medians": {field: statistics.median(r[field] for r in rounds) for field in ("query_ms", "database_cpu_ms")}},
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
