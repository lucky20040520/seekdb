"""Replay a table larger than the container limit through the published transport."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "protocol_check"))
from _transport import Session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--sort-only", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "protocol_check/config.json").read_text(encoding="utf-8"))
    config.update(memory="128m", cpus="1")
    session = Session(config, args.output.with_suffix(".stderr.log"), image=args.image)
    timings = {}

    def request(phase, sql, mode="exact", ok=True):
        started = time.monotonic()
        try:
            response = session.request(sql, mode)
            assert response["ok"] is ok, str(response)[:500]
            return response
        except Exception as error:
            raise RuntimeError(f"{phase}, request {session.sequence}, SQL prefix {sql[:140]!r}: {error}") from error
        finally:
            timings.setdefault(phase, []).append(time.monotonic() - started)

    count = 150000
    vector = "'[0,0,0,0,0,0,0,0]'"
    text = "'" + "x" * args.payload_bytes + "'"
    order = f"ORDER BY L2_DISTANCE(v,{vector}) LIMIT 10;"
    query = "SELECT id FROM t " + order
    passed = False
    try:
        session.start()
        request("schema", f"CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR({args.payload_bytes}),v VECTOR(8));")
        for start in range(0, count, 100):
            values = ",".join(f"({key},{text},{vector})" for key in range(start, start + 100))
            assert request("insert", "INSERT INTO t VALUES " + values + ";")["affected_rows"] == 100
            if (start + 100) % 25000 == 0:
                print(f"Inserted {start + 100} rows", flush=True)
        assert count * args.payload_bytes > 128 * 1024 * 1024
        if not args.sort_only:
            request("build_index", "CREATE VECTOR INDEX idx ON t(v);")
        assert request("sort", "SELECT id FROM t ORDER BY id DESC;")["rows"] == [
            [key] for key in range(count - 1, -1, -1)]
        if args.sort_only:
            session.shutdown()
            passed = True
            return
        for mode in ("exact", "ann"):
            assert request("topk", query, mode)["rows"] == [[key] for key in range(10)]
        request("transaction", "BEGIN;")
        request("transaction", "UPDATE t SET payload='changed',v='[2,0,0,0,0,0,0,0]' WHERE id < 4;")
        assert request("topk", query, "ann")["rows"] == [[key] for key in range(4, 14)]
        request("transaction", "ROLLBACK;")
        assert request("topk", query)["rows"] == [[key] for key in range(10)]
        request("transaction", "BEGIN;")
        assert request("delete", "DELETE FROM t WHERE id < 4;")["affected_rows"] == 4
        request("commit", "COMMIT;")
        session.shutdown()
        session.start()
        assert request("restart_scan", "SELECT id FROM t ORDER BY id;")["rows"] == [
            [key] for key in range(4, count)]
        request("duplicate_after_restart", f"INSERT INTO t VALUES(4,'duplicate',{vector});", ok=False)
        assert request("topk_after_restart", query, "ann")["rows"] == [[key] for key in range(4, 14)]
        session.shutdown()
        passed = True
    finally:
        session.close()
        result = {"image": args.image, "passed": passed, "rows": count,
                  "payload_bytes": count * args.payload_bytes,
                  "container_memory": config["memory"], "config": config,
                  "phases": {phase: {"requests": len(samples), "total_seconds": sum(samples),
                                    "max_seconds": max(samples)}
                             for phase, samples in timings.items()}}
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
