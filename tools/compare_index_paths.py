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


def run(image, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "protocol_check/config.json").read_text(encoding="utf-8"))
    config.update(memory="128m", cpus="1")
    count = 150_000
    payload = "x" * 1024
    result = {"image": image, "rows": count, "config": config, "passed": False,
              "phase_seconds": {}, "queries": [], "failure": None}
    phase = "startup"
    with tempfile.TemporaryDirectory(prefix="minivector-index-paths-") as parent:
        session = Session(config, output.with_suffix(".stderr.log"), image=image, data_root=parent)
        directory = Path(session.bind_data.name)
        # The database runs in a container with dropped capabilities.
        directory.chmod(0o777)

        def timed(name, action):
            nonlocal phase
            phase = name
            started = time.monotonic()
            try:
                return action()
            finally:
                result["phase_seconds"][name] = time.monotonic() - started

        def sql(statement, mode="exact"):
            response = session.request(statement, mode)
            assert response["ok"] is True, str(response)[:500]
            return response

        def load():
            sql("CREATE TABLE t(id BIGINT PRIMARY KEY, tag BIGINT, payload VARCHAR(1024), v VECTOR(8));")
            for start in range(0, count, 100):
                values = ",".join(f"({key},{key % 10},'{payload}','[{key % 17},0,0,0,0,0,0,0]')"
                                  for key in range(start, start + 100))
                assert sql("INSERT INTO t VALUES " + values + ";")["affected_rows"] == 100

        expected = [[key] for key in sorted(
            (key for key in range(1000, count) if key % 10 == 3),
            key=lambda key: (key % 17, key))[:10]]
        query = ("SELECT id FROM t WHERE tag=3 AND id>=1000 "
                 "ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0]') LIMIT 10;")

        def check_queries(stage):
            for mode in ("exact", "ann"):
                for index in range(3):
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
            check_queries("indexed")
            timed("shutdown", session.shutdown)
            timed("restart", session.start)
            check_queries("reopened")
            timed("shutdown_before_rebuild", session.shutdown)
            phase = "remove_derived_index_segments"
            derived = list(directory.glob("index-*.flat"))
            assert derived, "no derived index segments found"
            result["removed_index_segments"] = len(derived)
            for path in derived:
                path.unlink()
            timed("restart_with_rebuild", session.start)
            assert len(list(directory.glob("index-*.flat"))) == len(derived)
            check_queries("rebuilt")
            timed("final_shutdown", session.shutdown)
            result["passed"] = True
        except Exception as error:
            result["failure"] = {"phase": phase, "request": session.sequence, "reason": str(error)}
            raise
        finally:
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result), flush=True)
            session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.image, args.output)
