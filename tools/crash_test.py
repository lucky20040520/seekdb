#!/usr/bin/env python3
"""Crash-recovery probe for MiniVectorDB (formal cases include kill -9).

Scenario A: committed autocommit rows must survive SIGKILL.
Scenario B: an uncommitted transaction must not partially apply.
Scenario C: restart must be repeatable (kill during recovery, then recover again).

Usage: python3 tools/crash_test.py [--binary PATH]
Exit code 0 = all checks passed.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

BIN = "/mnt/d/桌面/round2/build/minivector"


class Server:
    def __init__(self, binary, data_dir, log):
        self.binary, self.data_dir, self.log = binary, data_dir, log
        self.proc = None
        self.ident = 0

    def start(self, timeout=60):
        self.proc = subprocess.Popen(
            [self.binary, "--data-dir", self.data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(self.log, "ab"), text=True, bufsize=1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server died before ready; see " + self.log)
            if line.strip() == '{"type":"ready"}':
                return
        raise TimeoutError("no ready line")

    def send(self, sql, mode="exact", timeout=60):
        self.ident += 1
        req = {"id": self.ident, "op": "execute", "sql": sql, "search_mode": mode}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("no response for: " + sql)
        return json.loads(line)

    def kill9(self):
        self.proc.kill()
        self.proc.wait()

    def shutdown(self):
        self.ident += 1
        self.proc.stdin.write(json.dumps({"id": self.ident, "op": "shutdown"}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.proc.stdin.close()
        rc = self.proc.wait(timeout=30)
        return json.loads(line), rc


def rows_of(resp):
    return [r for r in (resp.get("rows") or [])]


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail and not cond else ""))
    return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=BIN)
    args = ap.parse_args()

    ok = True
    root = tempfile.mkdtemp(prefix="crash-")
    data = os.path.join(root, "data")
    os.makedirs(data)
    log = os.path.join(root, "stderr.log")

    # ---------- A: committed rows survive SIGKILL ----------
    s = Server(args.binary, data, log)
    s.start()
    s.send("CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);")
    r = s.send("INSERT INTO t VALUES (1,10),(2,20);")
    ok &= check("A1 autocommit insert", r.get("affected_rows") == 2, str(r))
    s.kill9()
    s = Server(args.binary, data, log)
    s.start()
    r = s.send("SELECT id, v FROM t ORDER BY id;")
    ok &= check("A2 committed rows survive kill -9",
                rows_of(r) == [[1, 10], [2, 20]], str(r))

    # ---------- B: uncommitted transaction must not survive ----------
    s.send("BEGIN;")
    r = s.send("UPDATE t SET v = 999 WHERE id = 1;")
    ok &= check("B1 uncommitted update visible in txn", r.get("affected_rows") == 1, str(r))
    s.kill9()
    s = Server(args.binary, data, log)
    s.start()
    r = s.send("SELECT id, v FROM t ORDER BY id;")
    ok &= check("B2 uncommitted change rolled back after kill -9",
                rows_of(r) == [[1, 10], [2, 20]], str(r))

    # ---------- C: committed inside explicit txn survives ----------
    s.send("BEGIN;")
    s.send("INSERT INTO t VALUES (3,30);")
    r = s.send("COMMIT;")
    ok &= check("C1 commit ok", r.get("ok") is True, str(r))
    s.kill9()
    s = Server(args.binary, data, log)
    s.start()
    r = s.send("SELECT id, v FROM t ORDER BY id;")
    ok &= check("C2 committed txn row survives kill -9",
                rows_of(r) == [[1, 10], [2, 20], [3, 30]], str(r))

    # ---------- D: repeated recovery (kill again right after restart) ----------
    s.kill9()
    s = Server(args.binary, data, log)
    s.start()
    s.kill9()
    s = Server(args.binary, data, log)
    s.start()
    r = s.send("SELECT id, v FROM t ORDER BY id;")
    ok &= check("D1 data intact after two consecutive abrupt restarts",
                rows_of(r) == [[1, 10], [2, 20], [3, 30]], str(r))

    # ---------- E: vector + index path survives kill ----------
    s.send("CREATE TABLE d (id BIGINT PRIMARY KEY, e VECTOR(2));")
    s.send("INSERT INTO d VALUES (1,'[3,4]'),(2,'[0,0]');")
    s.send("CREATE VECTOR INDEX ix ON d(e);")
    s.kill9()
    s = Server(args.binary, data, log)
    s.start()
    r = s.send("SELECT id, L2_DISTANCE(e, '[0,0]') AS dist FROM d ORDER BY dist LIMIT 2;")
    ok &= check("E1 vector data and distance intact after kill -9",
                rows_of(r) == [[2, 0.0], [1, 5.0]], str(r))

    # ---------- F: clean shutdown rolls back open txn ----------
    s.send("BEGIN;")
    s.send("DELETE FROM t WHERE id >= 1;")
    resp, rc = s.shutdown()
    ok &= check("F1 shutdown ok and exit 0", resp.get("ok") is True and rc == 0, f"{resp} rc={rc}")
    s = Server(args.binary, data, log)
    s.start()
    r = s.send("SELECT id FROM t ORDER BY id;")
    ok &= check("F2 shutdown rolled back the open transaction",
                rows_of(r) == [[1], [2], [3]], str(r))
    s.shutdown()

    print()
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    print(f"artifacts: {root}")
    if ok:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
