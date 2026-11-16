"""Reproduce process death around catalog publication through the JSONL entry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading

from minivector.segments import Reader

WORKER = r"""
import os
import runpy
import sys

cut = sys.argv.pop(1)
replace = os.replace

def pause():
    print('CUT ' + cut, file=sys.stderr, flush=True)
    sys.stdin.readline()

def marked_replace(source, target):
    publish = os.path.basename(target) == 'catalog.json'
    if publish and cut == 'before':
        pause()
    replace(source, target)
    if publish and cut == 'after':
        pause()

os.replace = marked_replace
runpy.run_module('minivector', run_name='__main__')
"""


def pump(stream, events):
    for line in stream:
        events.put(line.rstrip())
    events.put(None)


class Session:
    def __init__(self, directory, cut="none"):
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", WORKER, cut, "--data-dir", str(directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        self.stdout, self.stderr = queue.Queue(), queue.Queue()
        self.threads = [
            threading.Thread(target=pump, args=(stream, events), daemon=True)
            for stream, events in [(self.process.stdout, self.stdout),
                                   (self.process.stderr, self.stderr)]
        ]
        for thread in self.threads:
            thread.start()

    def __enter__(self):
        try:
            assert self.receive() == {"type": "ready"}
        except BaseException:
            self.close()
            raise
        return self

    def send(self, ident, sql):
        self.process.stdin.write(json.dumps(
            {"id": ident, "op": "execute", "sql": sql, "search_mode": "exact"}
        ) + "\n")
        self.process.stdin.flush()

    def receive(self):
        line = self.stdout.get(timeout=10)
        assert line is not None, "unexpected EOF"
        return json.loads(line)

    def request(self, ident, sql):
        self.send(ident, sql)
        response = self.receive()
        assert response["id"] == ident and response["ok"] is True, response
        return response

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def __exit__(self, *exc):
        self.close()


def read_snapshot(root):
    raw = (root / "catalog.json").read_bytes()
    table = json.loads(raw)["t"]
    names = table.get("files", [table["file"]] if table["file"] else [])
    segments = []
    for name in names:
        path = root / name
        with Reader(path, table["columns"]) as reader:
            rows = [row for _, row in reader]
        segments.append({"name": name, "rows": rows})
    return {"catalog_sha256": hashlib.sha256(raw).hexdigest(), "segments": segments}


def trial(cut):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with Session(root) as session:
            session.request(1, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
            session.request(2, "INSERT INTO t VALUES (1),(2);")
        catalog = root / "catalog.json"
        previous = hashlib.sha256(catalog.read_bytes()).hexdigest()
        with Session(root, cut) as session:
            session.request(1, "BEGIN;")
            session.request(2, "DELETE FROM t WHERE id=1;")
            session.request(3, "INSERT INTO t VALUES (3);")
            session.send(4, "COMMIT;")
            if cut == "ack":
                response = session.receive()
                assert response["id"] == 4 and response["ok"] is True, response
                checkpoint = "client received COMMIT success"
            else:
                checkpoint = session.stderr.get(timeout=10)
                assert checkpoint == "CUT " + cut, checkpoint
                response = None
            pid = session.process.pid
        committed = hashlib.sha256(catalog.read_bytes()).hexdigest()
        pre_recovery = read_snapshot(root)
        with Session(root) as session:
            actual = session.request(1, "SELECT id FROM t ORDER BY id;")["rows"]
        expected = [[1], [2]] if cut == "before" else [[2], [3]]
        post_recovery = read_snapshot(root)
        disk_rows = sorted(row for segment in pre_recovery["segments"] for row in segment["rows"])
        result = {
            "cut": cut, "checkpoint": checkpoint, "killed_pid": pid,
            "commit_request_id": 4, "commit_response": response,
            "catalog_changed": previous != committed, "restarted_rows": actual,
            "expected_rows": expected, "pre_recovery": pre_recovery,
            "post_recovery": post_recovery,
            "passed": disk_rows == actual == expected and pre_recovery == post_recovery,
        }
        print(json.dumps(result), flush=True)
        assert result["passed"] and result["catalog_changed"] == (cut != "before")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cut", choices=("before", "after", "ack", "all"))
    args = parser.parse_args()
    for cut in ("before", "after", "ack") if args.cut == "all" else (args.cut,):
        trial(cut)


if __name__ == "__main__":
    main()
