"""Exercise identical indexed SQL and recovery paths on a specified image."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "protocol_check"))
from _transport import Session


def run(image, output, rounds):
    output.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "protocol_check/config.json").read_text(encoding="utf-8"))
    config.update(memory="128m", cpus="1")
    count = 150_000
    payload = "x" * 1024
    result = {"image": image, "rows": count, "config": config, "passed": False,
              "phase_seconds": {}, "queries": [], "requests": [], "failure": None}
    phase = "startup"
    with tempfile.TemporaryDirectory(prefix="minivector-index-paths-") as parent:
        session = Session(config, output.with_suffix(".stderr.log"), image=image, data_root=parent)
        directory = Path(session.bind_data.name)
        # The database runs in a container with dropped capabilities.
        directory.chmod(0o777)

        case_start = time.monotonic()

        def timed(name, action):
            nonlocal phase
            phase = name
            started = time.monotonic()
            try:
                return action()
            finally:
                result["phase_seconds"][name] = time.monotonic() - started

        def sql(statement, mode="exact"):
            started = time.monotonic()
            request = {"phase": phase, "sequence": session.sequence + 1,
                       "sql_prefix": statement[:100], "mode": mode}
            try:
                response = session.request(statement, mode)
            finally:
                request["seconds"] = time.monotonic() - started
                request["elapsed_seconds"] = time.monotonic() - case_start
                result["requests"].append(request)
            assert response["ok"] is True, str(response)[:500]
            return response

        def load():
            sql("CREATE TABLE t(id BIGINT PRIMARY KEY, tag BIGINT, payload VARCHAR(1024), v VECTOR(8));")
            for start in range(0, count, 100):
                values = ",".join(f"({key},{key % 10},'{payload}','[{key % 17},0,0,0,0,0,0,0]')"
                                  for key in range(start, start + 100))
                sql("BEGIN;")
                assert sql("INSERT INTO t VALUES " + values + ";")["affected_rows"] == 100
                sql("COMMIT;")

        expected = [[key] for key in sorted(
            (key for key in range(1000, count) if key % 10 == 3),
            key=lambda key: (key % 17, key))[:10]]
        query = ("SELECT id FROM t WHERE tag=3 AND id>=1000 "
                 "ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0]') LIMIT 10;")

        def check_queries(stage, queries):
            for index in range(queries):
                mode = "exact" if index % 2 == 0 else "ann"
                name = f"{stage}_{mode}_{index + 1}"
                response = timed(name, lambda: sql(query, mode))
                assert response["columns"] == ["id"] and response["rows"] == expected, name
                result["queries"].append(name)

        try:
            timed("startup", session.start)
            timed("load", load)
            result["row_file_bytes"] = sum(p.stat().st_size for p in directory.glob("rows-*.jsonl"))
            assert result["row_file_bytes"] > 128 * 1024 * 1024
            timed("create_index", lambda: sql("CREATE VECTOR INDEX idx ON t(v);"))
            check_queries("indexed", rounds // 2)
            timed("begin_uncommitted", lambda: sql("BEGIN;"))
            key = expected[0][0]
            timed("delete_uncommitted", lambda: sql(f"DELETE FROM t WHERE id={key};"))
            changed = timed("visible_uncommitted", lambda: sql(f"SELECT id FROM t WHERE id={key};"))
            assert changed["rows"] == []
            timed("kill_with_uncommitted_delete", session.crash)
            timed("restart", session.start)
            recovered = timed("verify_recovery", lambda: sql(f"SELECT id FROM t WHERE id={key};"))
            assert recovered["rows"] == [[key]]
            check_queries("reopened", rounds - rounds // 2)
            timed("final_shutdown", session.shutdown)
            result["passed"] = True
        except Exception as error:
            result["failure"] = {"phase": phase, "request": session.sequence, "reason": str(error)}
            raise
        finally:
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k not in {"requests", "phase_seconds", "queries"}}), flush=True)
            session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=240)
    args = parser.parse_args()
    run(args.image, args.output, args.rounds)
