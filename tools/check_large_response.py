"""Check large valid JSONL responses with the published transport limits."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "protocol_check"))
from _transport import Session


def run(image, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "protocol_check/config.json").read_text(encoding="utf-8"))
    report = {"image": image, "passed": False, "limits": config, "responses": []}
    session = Session(config, output.with_suffix(".stderr.log"), image=image)
    suffix = "\u6c49\U0001f600\n\\\"'"
    payload = "x" * (256 * 1024 - len(suffix)) + suffix
    literal = "'" + payload.replace("'", "''") + "'"
    count = 63
    phase = "startup"

    def sql(statement):
        response = session.request(statement)
        assert response["ok"] is True, str(response)[:500]
        return response

    def check_result(name, statement, changed=False):
        nonlocal phase
        phase = name
        started = time.monotonic()
        response = sql(statement)
        elapsed = time.monotonic() - started
        assert response["columns"] == ["id", "payload"]
        assert len(response["rows"]) == count
        for key, row in zip(range(count - 1, -1, -1), response["rows"]):
            assert row == [key, "short" if changed and key == 0 else payload], key
        size = len((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        assert 15 * 1024 * 1024 < size <= config["max_response_bytes"]
        report["responses"].append({"phase": name, "compact_utf8_bytes": size, "seconds": elapsed})
        print(report["responses"][-1], flush=True)

    try:
        session.start()
        phase = "load"
        sql("CREATE TABLE t(id BIGINT PRIMARY KEY, payload VARCHAR(262144));")
        for start in range(0, count, 4):
            values = ",".join(f"({key},{literal})" for key in range(start, min(start + 4, count)))
            assert sql("INSERT INTO t VALUES " + values + ";")["affected_rows"] == min(4, count - start)
        query = "SELECT id,payload FROM t ORDER BY id DESC;"
        check_result("sorted_large_result", query)
        phase = "transaction"
        sql("BEGIN;")
        sql("UPDATE t SET payload='short' WHERE id=0;")
        for index in range(3):
            check_result(f"transaction_large_result_{index + 1}", query, changed=True)
        phase = "rollback"
        sql("ROLLBACK;")
        check_result("after_rollback", query)
        phase = "shutdown"
        session.shutdown()
        phase = "restart"
        session.start()
        check_result("after_restart", query)
        phase = "final_shutdown"
        session.shutdown()
        report["passed"] = True
    except Exception as error:
        report["failure"] = {"phase": phase, "request": session.sequence, "reason": str(error)}
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.image, args.output)
