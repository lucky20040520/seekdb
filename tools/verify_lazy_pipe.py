"""Run real JSONL pipes with late reading and a large-success control."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

WORKER = r"""
import sys
from pathlib import Path
import minivector.jsonl as wire
from minivector.engine import DBError

class Database:
    path = Path(sys.argv[1])
    def close(self):
        pass

def failed(number):
    for index in range(1 if number == 1 else 100):
        yield [("FIRST" if number == 1 else "SECOND") + "x"*4096]
    raise DBError("TEST_" + str(number), "injected")

def execute(db, sql, mode):
    if sql.startswith("FAIL"):
        return ["payload"], failed(int(sql[-1]))
    if sql == "BIG":
        return ["payload"], iter([["x"*1000000]])
    return ["value"], iter([[7]])

wire.execute = execute
wire.serve(Database())
"""


def request(ident, sql):
    return {"id": ident, "op": "execute", "sql": sql, "search_mode": "exact"}


def send(process, items):
    for item in items:
        process.stdin.write(json.dumps(item) + "\n")
    process.stdin.flush()


def late_read(large):
    with tempfile.TemporaryDirectory() as root:
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", WORKER, root],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            items = [request("bad1", "FAIL1"), request("bad2", "FAIL2"),
                     request("good", "BIG" if large else "OK"),
                     {"id": "bye", "op": "shutdown"}]
            send(process, items)
            # All short requests have been written before any stdout read.
            thread = threading.Thread(target=process.wait, daemon=True)
            thread.start()
            thread.join(timeout=1)
            exited_before_read = process.poll() is not None
            stdout, stderr = process.communicate(timeout=15)
            thread.join(timeout=2)
            lines = stdout.splitlines()
            values = [json.loads(line) for line in lines]
            assert process.returncode == 0 and not stderr
            assert stdout.endswith("\n") and len(lines) == 5 and all(lines)
            assert values[0] == {"type": "ready"}
            assert [x["id"] for x in values[1:]] == ["bad1", "bad2", "good", "bye"]
            assert [x["ok"] for x in values[1:]] == [False, False, True, True]
            assert values[1]["error"]["code"] == "TEST_1"
            assert values[2]["error"]["code"] == "TEST_2"
            assert values[3]["rows"] == ([["x"*1000000]] if large else [[7]])
            assert not list(Path(root).iterdir())
            print(json.dumps({"large": large, "input_requests_written_before_read": 4,
                              "exited_before_read_after_1s": exited_before_read,
                              "stdout_bytes": len(stdout.encode("utf-8")), "lines": len(lines),
                              "ids": [x["id"] for x in values[1:]],
                              "exit_after_read": process.returncode, "temporary_files": 0,
                              "passed": True}))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    late_read(False)
    late_read(True)
