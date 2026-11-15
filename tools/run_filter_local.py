"""Run the standard exact workload locally with independent JSONL processes."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

from performance_checker.paired import prepare_truth
from performance_checker.workload import make_workload, verify_answer, verify_records

BASE = "e1561a7292443e4945b580cb23a24e399c2c18a5"


def phase(source, directory, workload, phase_name):
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
               PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    with subprocess.Popen(
        [sys.executable, "-m", "minivector", "--data-dir", str(directory)],
        cwd=source, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8"
    ) as process:
        try:
            assert json.loads(process.stdout.readline()) == {"type": "ready"}
            ident = 0

            def request(sql=None):
                nonlocal ident
                ident += 1
                message = {"id": ident, "op": "shutdown"}
                if sql is not None:
                    message.update(op="execute", sql=sql, search_mode="exact")
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
                response = json.loads(process.stdout.readline())
                assert response["ok"] is True and response["id"] == ident, response
                return response

            if phase_name == "verify":
                rows = []
                for start in range(0, 10000, 500):
                    rows.extend(request(
                        f"SELECT * FROM docs WHERE id>={start} AND id<{start+500} ORDER BY id;"
                    )["rows"])
                verify_records(rows, workload["records"])
                request()
                return

            request("CREATE TABLE docs(id BIGINT PRIMARY KEY,tag VARCHAR(32),v VECTOR(128));")
            started = time.perf_counter()
            for sql in workload["batches"]:
                request("BEGIN;")
                assert request(sql)["affected_rows"] == 100
                request("COMMIT;")
            write = time.perf_counter() - started
            answers = []
            query = None
            if phase_name == "query":
                request(workload["requests"][0]["sql"])
                started = time.perf_counter()
                for item in workload["requests"]:
                    answers.append(request(item["sql"]))
                query = time.perf_counter() - started
            request()
            assert process.wait(timeout=10) == 0
            return write, query, answers
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oracle-cache", type=Path, required=True)
    args = parser.parse_args()
    report = {"baseline_sha": BASE, "python": sys.version, "rounds": []}
    import numpy
    report["numpy"] = numpy.__version__
    with tempfile.TemporaryDirectory(dir=Path.cwd().parent) as root:
        root = Path(root)
        archive = subprocess.check_output(["git", "archive", "--format=zip", BASE, "minivector"])
        source = root / "baseline"
        with zipfile.ZipFile(io.BytesIO(archive)) as packed:
            packed.extractall(source)
        for index in range(3):
            workload = make_workload(data_seed=19017+index, query_seed=29017+index)
            prepare_truth(workload, args.oracle_cache)
            variants = [("baseline", source), ("candidate", Path.cwd())]
            for name, tree in reversed(variants) if index % 2 else variants:
                write_root = root / f"{name}-{index}-write"
                query_root = root / f"{name}-{index}-query"
                write, _, _ = phase(tree, write_root, workload, "write")
                _, query, answers = phase(tree, query_root, workload, "query")
                for directory in (write_root, query_root):
                    phase(tree, directory, workload, "verify")
                for item, answer, truth in zip(workload["requests"], answers, workload["truth"]):
                    assert verify_answer(answer, workload["records"], item["query"],
                                         item["tag"], truth=truth) == 1
                row = {"round": index+1, "variant": name, "write_seconds": write,
                       "query_seconds": query, "verified_queries": len(answers),
                       "persisted": True, "io_bytes": None, "memory_bytes": None}
                report["rounds"].append(row)
                args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
