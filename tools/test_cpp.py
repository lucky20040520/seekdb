"""Black-box JSONL regression tests for the C++ executable, with real restarts.

Examples (run from the repository root):
  python tools/test_cpp.py --docker-image minivector-cpp:test --output report.json
  python tools/test_cpp.py --output report.json --command ./build/minivector --data-dir {data}

--command must be last; {data} is replaced by a fresh case-specific directory.
No database implementation is imported. Docker cases use disposable named volumes;
kill tests kill the container, not merely the attached Docker CLI process.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path
import queue
import random
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid


if hasattr(sys.stdout, "reconfigure"):
    # Preserve a failed Unicode SQL probe even on Windows GBK consoles.
    sys.stdout.reconfigure(errors="backslashreplace")

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)  # The harness must itself accept the giant-ID test.


def strict_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AssertionError("duplicate response key: " + key)
            result[key] = value
        return result

    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise AssertionError("nonfinite response number")
        return value

    def invalid(text):
        raise AssertionError("nonstandard response constant: " + text)

    return json.loads(text, object_pairs_hook=pairs, parse_float=finite,
                      parse_constant=invalid)


def same(actual, expected):
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(same(a, b) for a, b in zip(actual, expected)))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(same(actual[k], v) for k, v in expected.items()))
    if type(expected) is float:
        return type(actual) in (float, int) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6)
    return type(actual) is type(expected) and actual == expected


def checked(command, timeout=30):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError("command failed: %r: %s" % (
            command, result.stderr.decode("utf-8", errors="replace")[-2000:]))
    return result.stdout.decode("utf-8", errors="strict")


class Session:
    def __init__(self, target):
        self.target = target
        self.timeout = target.args.timeout
        self.lines = queue.Queue()
        self.errors = deque(maxlen=30)
        self.sequence = 0
        self.name = "cpp-regression-" + uuid.uuid4().hex
        if target.args.docker_image:
            command = ["docker", "run", "--name", self.name, "-i", "--network", "none",
                       "--cpus", "2", "--memory", "2g", "--pids-limit", "128",
                       "--mount", "type=volume,source=" + target.volume + ",target=/data",
                       target.args.docker_image]
            if target.args.profile:
                command[-1:-1] = ["--env", "MINIVECTOR_PROFILE=1"]
            if target.args.screen_block is not None:
                command[-1:-1] = ["--env", "MINIVECTOR_SCREEN_BLOCK=" + str(target.args.screen_block)]
        else:
            command = [part.replace("{data}", str(target.directory)) for part in target.args.command]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def read_stdout():
            try:
                for line in self.process.stdout:
                    self.lines.put(line)
            finally:
                self.lines.put(None)

        def read_stderr():
            for line in self.process.stderr:
                self.errors.append(line.decode("utf-8", errors="replace").rstrip())

        self.threads = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=read_stderr, daemon=True)]
        for thread in self.threads:
            thread.start()
        target.sessions.append(self)
        assert self.read() == {"type": "ready"}, "incorrect startup handshake"

    def read(self):
        try:
            line = self.lines.get(timeout=self.timeout)
        except queue.Empty:
            raise AssertionError("response timeout; stderr=" + repr(list(self.errors)))
        if line is None:
            raise AssertionError("unexpected EOF; stderr=" + repr(list(self.errors)))
        assert line.endswith(b"\n"), "response missing physical newline"
        assert line.strip(), "empty protocol line"
        try:
            response = strict_loads(line.decode("utf-8", errors="strict"))
        except Exception as exc:
            raise AssertionError("invalid JSON response: %s; prefix=%r" % (exc, line[:500])) from exc
        assert isinstance(response, dict), "response must be an object"
        return response

    def raw(self, text):
        self.target.requests += 1
        self.target.last_request = text[:1200]
        payload = text.encode("utf-8") + b"\n"
        failures = []

        def write():
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except BaseException as exc:
                failures.append(exc)

        # A malformed-input target must not hang the harness by ceasing to read stdin.
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        response = self.read()
        writer.join(timeout=self.timeout)
        assert not writer.is_alive(), "request write timed out"
        assert not failures, "request write failed: " + repr(failures)
        assert type(response.get("ok")) is bool, "ok must be a JSON boolean"
        assert "id" in response, "response id missing"
        if not response["ok"]:
            assert isinstance(response.get("error"), dict), "error object missing"
            assert isinstance(response["error"].get("code"), str) and response["error"]["code"], "error code missing"
        return response

    def request(self, request):
        response = self.raw(json.dumps(request, ensure_ascii=False, allow_nan=False))
        assert same(response["id"], request["id"]), "request ID value or type changed"
        return response

    def sql(self, sql, *, rows=None, columns=None, affected=None, fail=False, mode="exact"):
        self.sequence += 1
        self.target.last_sql = sql[:1200]
        response = self.request({"id": self.sequence, "op": "execute", "sql": sql,
                                 "search_mode": mode})
        assert response["ok"] is not fail, "SQL outcome mismatch: %s => %r" % (sql[:1200], response)
        if rows is not None:
            assert same(response.get("rows"), rows), "rows mismatch: expected %r got %r" % (rows, response)
        if columns is not None:
            assert response.get("columns") == columns, "columns mismatch: " + repr(response)
        if affected is not None:
            assert type(response.get("affected_rows")) is int and response["affected_rows"] == affected, "affected rows mismatch"
        return response

    def stop(self):
        if self.process.poll() is not None:
            raise AssertionError("process terminated before shutdown")
        response = self.request({"id": "shutdown", "op": "shutdown"})
        assert response == {"id": "shutdown", "ok": True}, "shutdown shape mismatch"
        self.process.stdin.close()
        assert self.process.wait(timeout=self.timeout) == 0, "nonzero graceful exit"
        for thread in self.threads:
            thread.join(timeout=self.timeout)
        assert self.lines.get(timeout=self.timeout) is None and self.lines.empty(), "trailing stdout after shutdown"

    def kill(self):
        if self.target.args.docker_image:
            checked(["docker", "kill", "--signal", "KILL", self.name], self.timeout)
        else:
            self.process.kill()
        self.process.wait(timeout=self.timeout)

    def cleanup(self):
        if self.target.args.docker_image:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=30)
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=self.timeout)
        for thread in self.threads:
            thread.join(timeout=self.timeout)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class Target:
    def __init__(self, args):
        self.args = args
        self.sessions = []
        self.requests = 0
        self.last_sql = None
        self.last_request = None
        self.temporary = tempfile.TemporaryDirectory(prefix="cpp-regression-")
        self.directory = Path(self.temporary.name)
        self.volume = "cpp-regression-" + uuid.uuid4().hex
        if args.docker_image:
            checked(["docker", "volume", "create", self.volume])

    def start(self):
        return Session(self)

    def remove_index_files(self):
        if self.args.docker_image:
            output = checked(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh",
                              "--mount", "type=volume,source=" + self.volume + ",target=/data",
                              self.args.docker_image, "-c",
                              "find /data -maxdepth 1 -type f -name 'index-*.flat' -print -delete"])
            files = output.splitlines()
        else:
            files = list(self.directory.glob("index-*.flat"))
            for file in files:
                file.unlink()
        assert files, "no materialized index files found to exercise rebuilding"
        return len(files)

    def cleanup(self):
        errors = []
        for session in self.sessions:
            try:
                session.cleanup()
            except Exception as exc:
                errors.append(str(exc))
        if self.args.docker_image:
            try:
                checked(["docker", "volume", "rm", self.volume])
            except Exception as exc:
                errors.append(str(exc))
        self.temporary.cleanup()
        if errors:
            raise AssertionError("cleanup failed: " + repr(errors))


def create(session):
    session.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2));")


def crud_and_int64(target):
    s = target.start()
    create(s)
    s.sql("INSERT INTO t(v,id,tag) VALUES('[3,4]',9223372036854775807,'汉😀'),([0,0],-9223372036854775808,'a'';b');", affected=2)
    s.sql("SELECT id,tag,v FROM t ORDER BY id;", columns=["id", "tag", "v"],
          rows=[[-9223372036854775808, "a';b", [0., 0.]], [9223372036854775807, "汉😀", [3., 4.]]])
    s.sql("UPDATE t SET tag='新',v=[1,2] WHERE id>=1 AND id<=9223372036854775807;", affected=1)
    s.sql("SELECT tag,L2_DISTANCE(v,'[1,2]') AS d FROM t WHERE tag='新';", rows=[["新", 0.]])
    s.sql("DELETE FROM t WHERE id<0;", affected=1)
    s.sql("DELETE FROM t WHERE id<0;", affected=0)
    s.sql("SELECT id FROM t;", rows=[[9223372036854775807]])
    s.stop()


def type_errors_atomicity(target):
    s = target.start()
    s.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(2),v VECTOR(2));")
    s.sql("INSERT INTO t VALUES(1,'汉😀',[0,0]);", affected=1)
    literals = ["9223372036854775808,'a',[0,0]", "-9223372036854775809,'a',[0,0]",
                "1.0,'a',[0,0]", "2,'abc',[0,0]", "2,'汉😀x',[0,0]", "2,'a',[0]",
                "2,'a',[NaN,0]", "2,'a',[Infinity,0]", "2,'a',[1e100,0]", "2,'a',['x',0]"]
    for literal in literals:
        s.sql("INSERT INTO t VALUES(" + literal + ");", fail=True)
    s.sql("INSERT INTO t VALUES(2,'ok',[0,0]),(3,'汉😀x',[0,0]);", fail=True)
    s.sql("UPDATE t SET tag='汉😀x';", fail=True)
    s.sql("SELECT * FROM t;", rows=[[1, "汉😀", [0., 0.]]])
    s.sql("SELECT id FROM t WHERE tag='汉😀x';", rows=[])
    s.stop()


def invalid_sql_and_schema(target):
    s = target.start()
    create(s)
    statements = ["CREATE TABLE bad(id BIGINT);", "CREATE TABLE bad(id BIGINT PRIMARY KEY,v VECTOR(0));",
                  "CREATE TABLE bad(id BIGINT PRIMARY KEY,v VECTOR(2),w VECTOR(2));",
                  "CREATE TABLE bad(id BIGINT PRIMARY KEY,id VARCHAR(2));", "CREATE TABLE bad(x FLOAT PRIMARY KEY);",
                  "SELECT missing FROM t;", "SELECT * FROM t WHERE missing=1;", "SELECT * FROM t LIMIT -1;",
                  "SELECT * FROM t LIMIT 1.5;", "SELECT * FROM t JOIN t;", "UPDATE t SET id=3;",
                  "DELETE FROM t WHERE id != 1;", "SELECT * FROM t; DELETE FROM t;", "SELECT COUNT(*) FROM t;",
                  "CREATE VECTOR INDEX bad ON t(tag);", "COMMIT;", "ROLLBACK;"]
    for sql in statements:
        s.sql(sql, fail=True)
    s.sql("SELECT id FROM t;", rows=[], columns=["id"])
    s.sql("CREATE TABLE b(x BIGINT,PRIMARY KEY(x));")
    s.stop()


def schema_permutations(target):
    s = target.start()
    definitions = {"id": "id BIGINT PRIMARY KEY", "tag": "tag VARCHAR(2)", "v": "v VECTOR(2)"}
    values = {"id": "1", "tag": "'ab'", "v": "[3,4]"}
    for i, names in enumerate(itertools.permutations(definitions)):
        table = "t%d" % i
        s.sql("CREATE TABLE " + table + "(" + ",".join(definitions[n] for n in names) + ");")
        s.sql("INSERT INTO " + table + " VALUES(" + ",".join(values[n] for n in names) + ");")
        for indexed in (False, True):
            if indexed:
                s.sql("CREATE VECTOR INDEX i%d ON %s(v);" % (i, table))
            for mode in ("exact", "ann"):
                s.sql("SELECT id,tag,L2_DISTANCE(v,[0,0]) AS d FROM " + table + " WHERE tag='ab' ORDER BY d LIMIT 10;",
                      rows=[[1, "ab", 5.]], mode=mode)
    s.stop()


def float32_and_sorting(target):
    s = target.start()
    create(s)
    s.sql("INSERT INTO t VALUES(3,'yes',[3,4]),(2,'no',[0,0]),(1,'yes',[-3,-4]);")
    for indexed in (False, True):
        if indexed:
            s.sql("CREATE VECTOR INDEX idx ON t(v);")
        for mode in ("exact", "ann"):
            s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t WHERE tag='yes' ORDER BY d LIMIT 10;", rows=[[1, 5.], [3, 5.]], mode=mode)
            s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) DESC LIMIT 3;", rows=[[1], [3], [2]], mode=mode)
            s.sql("SELECT id FROM t WHERE tag='absent' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;", rows=[], columns=["id"], mode=mode)
            s.sql("SELECT * FROM t LIMIT 0;", rows=[], columns=["id", "tag", "v"], mode=mode)
    s.sql("UPDATE t SET v=[-0.0,0.0];")
    s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) DESC LIMIT 3;", rows=[[1], [2], [3]])
    s.sql("DELETE FROM t;")
    s.sql("INSERT INTO t VALUES(2,'a',[16777216,0]),(1,'a',[16777217,0]);")
    s.sql("SELECT id,L2_DISTANCE(v,[16777216,0]) AS d FROM t ORDER BY d LIMIT 2;", rows=[[1, 0.], [2, 0.]])
    s.sql("UPDATE t SET v=[3.4028234663852886e38,-3.4028234663852886e38];")
    response = s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 2;")
    assert [row[0] for row in response["rows"]] == [1, 2]
    assert all(math.isfinite(row[1]) and row[1] > 1e38 for row in response["rows"])
    s.sql("DELETE FROM t;")
    s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;", rows=[])
    s.stop()


def transaction_combinations(target):
    s = target.start()
    for table in ("a", "b"):
        s.sql("CREATE TABLE %s(id BIGINT PRIMARY KEY,s VARCHAR(2),v VECTOR(2));" % table)
        s.sql("INSERT INTO %s VALUES(1,'x','[1,0]'),(2,'y','[2,0]');" % table)
    s.sql("CREATE VECTOR INDEX ia ON a(v);")
    for commit in (False, True):
        s.sql("BEGIN;")
        s.sql("BEGIN;", fail=True)
        s.sql("CREATE VECTOR INDEX ib ON b(v);")
        s.sql("UPDATE a SET v='[0,0]' WHERE id=2;")
        s.sql("DELETE FROM b WHERE id=1;")
        s.sql("INSERT INTO b VALUES(3,'z','[0,0]'),(2,'z','[0,0]');", fail=True)
        s.sql("INSERT INTO b VALUES(4,'z','[0,0]');")
        for mode in ("exact", "ann"):
            s.sql("SELECT id FROM a ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;", rows=[[2], [1]], mode=mode)
            s.sql("SELECT id FROM b ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;", rows=[[4], [2]], mode=mode)
        s.sql("COMMIT;" if commit else "ROLLBACK;")
        s.stop()
        s = target.start()
        for mode in ("exact", "ann"):
            s.sql("SELECT id FROM a ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;", rows=[[2], [1]] if commit else [[1], [2]], mode=mode)
            s.sql("SELECT id FROM b ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;", rows=[[4], [2]] if commit else [[1], [2]], mode=mode)
    s.stop()


def recovery(target, action):
    s = target.start()
    create(s)
    s.sql("CREATE TABLE u(id BIGINT PRIMARY KEY);")
    s.sql("INSERT INTO t VALUES(1,'old',[1,0]),(2,'old',[2,0]);")
    s.sql("CREATE VECTOR INDEX idx ON t(v);")
    s.sql("BEGIN;")
    s.sql("UPDATE t SET v=[0,0] WHERE id=2;")
    s.sql("DELETE FROM t WHERE id=1;")
    s.sql("INSERT INTO t VALUES(3,'new',[3,0]);")
    s.sql("INSERT INTO u VALUES(8);")
    if action == "commit_kill":
        s.sql("COMMIT;")
    if action == "shutdown_uncommitted":
        s.stop()
    else:
        s.kill()
    committed = action == "commit_kill"
    expected = [[2, "old", [0., 0.]], [3, "new", [3., 0.]]] if committed else [[1, "old", [1., 0.]], [2, "old", [2., 0.]]]
    for _ in range(2):
        s = target.start()
        s.sql("SELECT * FROM t ORDER BY id;", rows=expected)
        s.sql("SELECT id FROM u;", rows=[[8]] if committed else [])
        for mode in ("exact", "ann"):
            s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;", rows=[[r[0]] for r in expected], mode=mode)
        s.stop()


def missing_index_rebuild(target):
    s = target.start()
    create(s)
    s.sql("INSERT INTO t VALUES(3,'b',[0,2]),(1,'a',[0,0]),(2,'a',[1,0]);")
    s.sql("CREATE VECTOR INDEX idx ON t(v);")
    s.stop()
    target.remove_index_files()
    s = target.start()
    for mode in ("exact", "ann"):
        s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;", rows=[[1], [2], [3]], mode=mode)
    s.stop()
    # Rebuilding should have produced a materialized index, not silently abandoned it.
    target.remove_index_files()
    s = target.start()
    s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;", rows=[[1], [2], [3]], mode="ann")
    s.stop()


def protocol_boundaries(target):
    s = target.start()
    create(s)
    ids = [None, False, True, 0, -1, 9007199254740993, "1", "汉😀\n;", [1, "x"], {"nested": [True, None]}, int("9" * 5000)]
    for ident in ids:
        response = s.request({"id": ident, "op": "execute", "sql": "SELECT id FROM t;"})
        assert response["ok"] and response["rows"] == []
    for raw in ('{"id":1,"id":2,"op":"execute","sql":"SELECT id FROM t;"}',
                '{"id":1,"op":"execute","sql":"SELECT id FROM t;","x":{"a":1,"a":2}}',
                '{"id":NaN}', '{"id":1e999}', '{"id":[1e999]}',
                '{"id":{"n":-1e999}}', '[]', '{"op":"execute"}', 'not-json'):
        assert not s.raw(raw)["ok"], "invalid request accepted: " + raw
        s.sql("SELECT id FROM t;", rows=[])
    for request in ({"id": "bad-op", "op": "unknown"},
                    {"id": "bad-mode", "op": "execute", "sql": "SELECT id FROM t;", "search_mode": "wrong"},
                    {"id": "bad-type", "op": "execute", "sql": 123}):
        assert not s.request(request)["ok"]
    s.sql("INSERT INTO t VALUES(1,'a'';b',[0,0]);", affected=1)
    s.sql("SELECT tag FROM t;", rows=[["a';b"]])
    s.stop()


def long_input_recovery(target):
    s = target.start()
    length = target.args.long_string_bytes
    s.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(%d));" % length)
    payload = "x" * length
    s.sql("INSERT INTO t VALUES(1,'" + payload + "');", affected=1)
    response = s.sql("SELECT payload FROM t WHERE id=1;")
    assert response["rows"] == [[payload]], "large valid string was truncated or changed"
    s.sql("INSERT INTO t VALUES(2,'ok'),(3,'" + payload + "x');", fail=True)
    s.sql("SELECT id FROM t ORDER BY id;", rows=[[1]])
    s.stop()
    s = target.start()
    response = s.sql("SELECT payload FROM t WHERE id=1;")
    assert response["rows"] == [[payload]], "large string changed after restart"
    s.stop()


def randomized_index_truth(target):
    s = target.start()
    create(s)
    s.sql("CREATE VECTOR INDEX idx ON t(v);")
    rng = random.Random(991)
    committed = {}
    for step in range(24):
        staged = {key: list(value) for key, value in committed.items()}
        s.sql("BEGIN;")
        primary = step + 1
        vector = [rng.randrange(-5, 6), rng.randrange(-5, 6)]
        tag = "a" if step % 2 else "b"
        s.sql("INSERT INTO t VALUES(%d,'%s',%s);" % (primary, tag, json.dumps(vector)))
        staged[primary] = [tag, vector]
        if primary - 1 in staged:
            s.sql("UPDATE t SET v=[2,3] WHERE id=%d;" % (primary - 1))
            staged[primary - 1][1] = [2, 3]
        if step % 3 == 0:
            s.sql("DELETE FROM t WHERE id=%d;" % (primary - 2))
            staged.pop(primary - 2, None)
        for filtered in (False, True):
            pool = [(key, math.hypot(value[1][0] - 1, value[1][1] - 1))
                    for key, value in staged.items() if not filtered or value[0] == "b"]
            for descending in (False, True):
                truth = sorted(pool, key=lambda item: (-item[1] if descending else item[1], item[0]))[:10]
                sql = "SELECT id,L2_DISTANCE(v,[1,1]) AS d FROM t " + ("WHERE tag='b' " if filtered else "") + "ORDER BY d " + ("DESC " if descending else "ASC ") + "LIMIT 10;"
                for mode in ("exact", "ann"):
                    s.sql(sql, rows=[[key, distance] for key, distance in truth], mode=mode)
        rollback = step % 4 == 0
        s.sql("ROLLBACK;" if rollback else "COMMIT;")
        if not rollback:
            committed = staged
    s.stop()
    s = target.start()
    s.sql("SELECT id,tag,v FROM t ORDER BY id;", rows=[[key, value[0], [float(x) for x in value[1]]]
                                                       for key, value in sorted(committed.items())])
    s.stop()


def contiguous_cache_and_filters(target):
    s = target.start()
    # Vector and primary-key columns deliberately have non-default positions.
    s.sql("CREATE TABLE t(v VECTOR(3),tag VARCHAR(8),id BIGINT PRIMARY KEY,n BIGINT);")
    records = {i: [[float(i % 7), float(i % 3), -2.0], ("a", "中", "c")[i % 3], i, i % 5]
               for i in range(-60, 60)}
    values = ["(%s,'%s',%d,%d)" % (json.dumps(row[0]), row[1], row[2], row[3])
              for row in records.values()]
    s.sql("INSERT INTO t VALUES" + ",".join(values) + ";", affected=120)

    def check(where="", predicate=lambda row: True, limit=10):
        pool = [row for row in records.values() if predicate(row)]
        truth = sorted(pool, key=lambda row: (math.fsum(x*x for x in row[0]), row[2]))[:limit]
        s.sql("SELECT * FROM t " + ("WHERE " + where if where else "")
              + " ORDER BY L2_DISTANCE(v,[0,0,0]) LIMIT %d;" % limit, rows=truth)

    for _ in range(2):
        check()
        check("tag='中'", lambda row: row[1] == "中")
        check("tag='中' AND n=2 AND id>=0", lambda row: row[1] == "中" and row[3] == 2 and row[2] >= 0)
        check("tag='missing'", lambda row: False)
        check("tag='a' AND tag='c'", lambda row: False)
        check("n>=2 AND id<0", lambda row: row[3] >= 2 and row[2] < 0)
    # More distinct predicates than the 32-list limit, including empty lists.
    for i in range(-40, 40):
        check("id=%d" % i, lambda row, rid=i: row[2] == rid)
    check("tag='中'", lambda row: row[1] == "中")
    saved = {key: list(row) for key, row in records.items()}
    s.sql("BEGIN;")
    s.sql("UPDATE t SET tag='中',v=[0,0,0] WHERE id=-60;", affected=1)
    records[-60] = [[0.0, 0.0, 0.0], "中", -60, 0]
    check("tag='中'", lambda row: row[1] == "中")
    s.sql("DELETE FROM t WHERE id=1;", affected=1)
    records.pop(1)
    check("tag='中'", lambda row: row[1] == "中")
    s.sql("ROLLBACK;")
    records = saved
    check("tag='中'", lambda row: row[1] == "中")
    s.sql("BEGIN;")
    s.sql("DELETE FROM t WHERE tag='中';", affected=40)
    records = {key: row for key, row in records.items() if row[1] != "中"}
    check("tag='中'", lambda row: False)
    s.sql("COMMIT;")
    check("tag='中'", lambda row: False)
    s.sql("INSERT INTO t VALUES([0,0,0],'中',200,2);", affected=1)
    records[200] = [[0.0, 0.0, 0.0], "中", 200, 2]
    check("tag='中'", lambda row: row[1] == "中")
    s.sql("CREATE TABLE other(id BIGINT PRIMARY KEY,v VECTOR(1));")
    s.sql("INSERT INTO other VALUES(1,[1]);")
    s.sql("SELECT * FROM other ORDER BY L2_DISTANCE(v,[0]) LIMIT 1;", rows=[[1, [1.0]]])
    check()
    s.stop()
    s = target.start()
    check("tag='中'", lambda row: row[1] == "中")
    s.stop()


def wide_records_fallback(target):
    s = target.start()
    s.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(65536),v VECTOR(2));")
    # More than the 16 MiB cache cap, with enough sort keys to require the 2 MiB
    # external-sort path. Only numeric projections are returned over JSONL.
    s.sql("BEGIN;")
    for start in range(1, 501, 100):
        values = ["(%d,'%s',[ %d,0])" % (i, ("%04d" % (500-i)) + "x" * 65532, i)
                  for i in range(start, start + 100)]
        s.sql("INSERT INTO t VALUES" + ",".join(values) + ";", affected=100)
    s.sql("COMMIT;")
    for mode in ("exact", "ann"):
        # LIMIT 1 fits the heap even for wide rows, so cache construction is
        # attempted and must abandon its partial build at the shared budget.
        s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1;", rows=[[1]], mode=mode)
        s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;",
              rows=[[i] for i in range(1, 11)], mode=mode)
        s.sql("SELECT id FROM t WHERE id>=250 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;",
              rows=[[i] for i in range(250, 260)], mode=mode)
    s.sql("SELECT id FROM t ORDER BY payload;", rows=[[i] for i in range(500, 0, -1)])
    s.sql("SELECT id FROM t ORDER BY payload DESC;", rows=[[i] for i in range(1, 501)])
    s.sql("SELECT id FROM t ORDER BY payload LIMIT 1000;", rows=[[i] for i in range(500, 0, -1)])
    s.stop()


def distance_reuse_and_residuals(target):
    s = target.start()
    create(s)
    records = [[3, "a", [0., 0.]], [1, "a", [0., 0.]], [5, "b", [2., 0.]],
               [2, "a", [1., 0.]], [4, "a", [0., 2.]]]
    s.sql("INSERT INTO t VALUES" + ",".join("(%d,'%s',%s)" % (r[0], r[1], json.dumps(r[2])) for r in records) + ";")

    def check():
        for where, predicate in (("tag='a'", lambda r: r[1] == "a"),
                                 ("id=2 AND tag='b'", lambda r: False),
                                 ("tag='a' AND id=2", lambda r: r[0] == 2),
                                 ("id>=2 AND tag='a'", lambda r: r[0] >= 2 and r[1] == "a"),
                                 ("tag='a' AND tag='a'", lambda r: r[1] == "a")):
            for descending in (False, True):
                truth = sorted((r for r in records if predicate(r)),
                               key=lambda r: ((-1 if descending else 1)*math.hypot(*r[2]), r[0]))[:3]
                expected = [[r[0], math.hypot(*r[2]), math.hypot(*r[2]),
                             math.hypot(r[2][0]-2, r[2][1]), r[2], r[1]] for r in truth]
                s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS d,L2_DISTANCE(v,'[0,0]'),"
                      "L2_DISTANCE(v,[2,0]),v,tag FROM t WHERE " + where + " ORDER BY d "
                      + ("DESC" if descending else "ASC") + " LIMIT 3;", rows=expected)
        # Same projection expression, different ORDER BY expression: no reuse.
        s.sql("SELECT id,L2_DISTANCE(v,[2,0]) FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2;",
              rows=[[1, 2.0], [3, 2.0]])
        # External-sort/ordinary scan, including distance projection without order.
        s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d;",
              rows=[[1, 0.0], [3, 0.0], [2, 1.0], [4, 2.0], [5, 2.0]])
        s.sql("SELECT L2_DISTANCE(v,[2,0]) FROM t WHERE id=2;", rows=[[1.0]])

    check()
    s.sql("CREATE VECTOR INDEX iv ON t(v);")
    check()
    s.stop()


def duplicate_alias_ordering(target):
    """Pin the submitted Python version's first/last duplicate-alias behavior."""
    s = target.start()
    s.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,v VECTOR(2))")
    s.sql("INSERT INTO t VALUES(1,30,[0,0]),(2,10,[10,0]),(3,20,[5,0])", affected=3)
    distance_rows = {1: [1, 0.0, 10.0], 2: [2, 10.0, 0.0], 3: [3, 5.0, 5.0]}
    scalar_rows = {1: [1, 0.0, 30], 2: [2, 10.0, 10], 3: [3, 5.0, 20]}
    # Expected IDs observed in f1b9f4e, respectively without and with an index.
    order_cases = [
        (" LIMIT 0", [], []),
        (" LIMIT 1", [1], [1]),
        (" LIMIT 2", [1, 3], [1, 3]),
        (" DESC LIMIT 1", [1], [2]),
        (" DESC LIMIT 2", [1, 3], [2, 3]),
        (" LIMIT 4097", [2, 3, 1], [1, 3, 2]),
        ("", [2, 3, 1], [1, 3, 2]),
    ]

    def check(indexed):
        for mode in ("exact", "ann"):
            for suffix, without_index, with_index in order_cases:
                ids = with_index if indexed else without_index
                for last, rows in [("L2_DISTANCE(v,[10,0])", distance_rows), ("n", scalar_rows)]:
                    s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS x," + last + " AS x FROM t ORDER BY x" + suffix,
                          columns=["id", "x", "x"], rows=[rows[i] for i in ids], mode=mode)
            s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS x,L2_DISTANCE(v,[10,0]) AS x FROM t WHERE id>=2 ORDER BY x LIMIT 1",
                  rows=[[3, 5.0, 5.0]], mode=mode)
            s.sql("SELECT id,n AS x,L2_DISTANCE(v,[10,0]) AS x FROM t ORDER BY x LIMIT 1",
                  rows=[[2, 10, 0.0]], mode=mode)
            s.sql("SELECT id,L2_DISTANCE(v,[0,0]),L2_DISTANCE(v,[10,0]) FROM t ORDER BY `l2_distance` LIMIT 1",
                  columns=["id", "l2_distance", "l2_distance"], rows=[[1, 0.0, 10.0]], mode=mode)
            s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS `Ä`,L2_DISTANCE(v,[10,0]) AS `ä` FROM t ORDER BY `Ä` LIMIT 1",
                  columns=["id", "ä", "ä"], rows=[[1, 0.0, 10.0]], mode=mode)
            s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS x,v AS x FROM t ORDER BY x LIMIT 1",
                  rows=[[1, 0.0, [0.0, 0.0]]], mode=mode)
            s.sql("SELECT v AS x,n AS x FROM t ORDER BY x LIMIT 0", fail=True, mode=mode)
            s.sql("SELECT n AS x,v AS x FROM t ORDER BY x LIMIT 0", columns=["x", "x"], rows=[], mode=mode)
            s.sql("SELECT n AS x,v AS x FROM t WHERE id=-1 ORDER BY x", rows=[], mode=mode)
            s.sql("SELECT n AS x,v AS x FROM t ORDER BY x LIMIT 1", fail=True, mode=mode)
            s.sql("SELECT id,L2_DISTANCE(v,[0,0]) AS x,v AS x FROM t ORDER BY x DESC LIMIT 1",
                  rows=[[2, 10.0, [10.0, 0.0]]] if indexed else None, fail=not indexed, mode=mode)

    check(False)
    s.sql("CREATE VECTOR INDEX vi ON t(v)")
    check(True)
    s.stop()
    s = target.start()
    check(True)

    # Python's full-row heap budget is different from the compact C++ cache.
    # LIMIT 4 must take the ordinary (last-alias) path without an index here.
    dimension = 16384
    zero = "[" + ",".join(["0"] * dimension) + "]"
    ten = "[10," + ",".join(["0"] * (dimension - 1)) + "]"
    five = "[5," + ",".join(["0"] * (dimension - 1)) + "]"
    s.sql(f"CREATE TABLE wide_alias(id BIGINT PRIMARY KEY,v VECTOR({dimension}))")
    s.sql(f"INSERT INTO wide_alias VALUES(1,{zero}),(2,{ten}),(3,{five})", affected=3)
    query = f"SELECT id,L2_DISTANCE(v,{zero}) AS x,L2_DISTANCE(v,{ten}) AS x FROM wide_alias ORDER BY x"
    s.sql(query + " LIMIT 1", rows=[[1, 0.0, 10.0]])
    s.sql(query + " LIMIT 4", rows=[distance_rows[i] for i in [2, 3, 1]])
    s.sql("CREATE VECTOR INDEX wide_vi ON wide_alias(v)")
    s.sql(query + " LIMIT 4", rows=[distance_rows[i] for i in [1, 3, 2]])
    s.stop()


def unicode_identifiers(target):
    s = target.start()
    def ident(text):
        return "`" + text.replace("`", "``") + "`"
    names = ["Ä", "İ", "ΟΣ", "ẞ", "АБВ", "ＡＢ", "𐐀", "Ა", "A\u0345Σ", "Ο`Σ"]
    s.sql("CREATE TABLE `TÄ`(id BIGINT PRIMARY KEY," +
          ",".join(ident(name) + " BIGINT" for name in names) + ",`ВЕКТОР` VECTOR(2),s VARCHAR(32))")
    literal = "Ä İ ΟΣ\u00a0１２٣"
    s.sql("INSERT INTO `tä`(id," + ",".join(ident(name.lower()) for name in names) + ",`вектор`,s) VALUES(1," +
          ",".join(str(i) for i in range(len(names))) + ",[3,4],'" + literal + "')", affected=1)
    expected = list(range(len(names)))
    s.sql("SELECT " + ",".join(ident(name) for name in names) + " FROM `TÄ`",
          columns=[name.lower() for name in names], rows=[expected])
    s.sql("SELECT " + ",".join(ident(name.lower()) for name in names) + " FROM `tä`", rows=[expected])
    for name in names:
        s.sql("SELECT id AS " + ident(name) + " FROM `tä` ORDER BY " + ident(name.lower()),
              columns=[name.lower()], rows=[[1]])
    s.sql("SELECT s FROM `tä`", rows=[[literal]])
    s.sql("SELECT id FROM `tä` WHERE `ä`=0 AND `ος`=2 ORDER BY `i̇`", rows=[[1]])
    for indexed in (False, True):
        if indexed:
            s.sql("CREATE VECTOR INDEX `ÍΝDEX` ON `TÄ`(`ВЕКТОР`)")
            s.sql("CREATE VECTOR INDEX `íνdex` ON `tä`(`вектор`)", fail=True)
        s.sql("SELECT id,L2_DISTANCE(`ВЕКТОР`,[0,0]) AS `ÖΣ` FROM `tä` ORDER BY `öς` LIMIT 1",
              columns=["id", "öς"], rows=[[1,5.0]], mode="ann")
    for i, name in enumerate(["Ä", "İ", "ΟΣ", "𐐀"]):
        s.sql(f"CREATE TABLE bad{i}(id BIGINT PRIMARY KEY,{ident(name)} BIGINT,{ident(name.lower())} BIGINT)", fail=True)
    # Python lower() is neither casefold() nor Unicode normalization.
    s.sql("CREATE TABLE distinct_names(id BIGINT PRIMARY KEY,`ß` BIGINT,ss BIGINT,`é` BIGINT,`é` BIGINT,`Ａ` BIGINT,a BIGINT)")
    s.sql("INSERT INTO distinct_names VALUES(1,2,3,4,5,6,7)", affected=1)
    s.sql("SELECT `ß`,ss,`é`,`é`,`ａ`,a FROM distinct_names", rows=[[2,3,4,5,6,7]])
    s.sql("SELECT Ä FROM `tä`", fail=True)
    s.sql("BEGIN")
    s.sql("UPDATE `tä` SET `ä`=99,`вектор`=[0,0] WHERE id=1", affected=1)
    s.sql("SELECT `Ä` FROM `tä`", rows=[[99]])
    s.kill()
    s = target.start()
    s.sql("SELECT `Ä`,s FROM `tä`", columns=["ä","s"], rows=[[0,literal]])
    s.sql("SELECT id FROM `tä` ORDER BY L2_DISTANCE(`вектор`,[0,0]) LIMIT 1", rows=[[1]])
    s.sql("BEGIN")
    s.sql("UPDATE `TÄ` SET `Ä`=99 WHERE id=1", affected=1)
    s.sql("COMMIT")
    s.stop()
    s = target.start()
    s.sql("SELECT `ä` FROM `tä`", rows=[[99]])
    s.sql("DELETE FROM `TÄ` WHERE `Ä`=99", affected=1)
    s.sql("SELECT `Ä` FROM `tä`", columns=["ä"], rows=[])
    s.stop()


def unicode_lexing(target):
    s = target.start()
    s.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(١٦),v VECTOR(２))")
    s.sql("INSERT INTO t VALUES(١,+１２,'１２٣\u00a0Ä',[١.٥e+١,.٥])", affected=1)
    s.sql("SELECT * FROM t", rows=[[1,12,"１２٣\u00a0Ä",[15.0,0.5]]])
    whitespace = list(range(9,14)) + list(range(28,33)) + [0x85,0xa0,0x1680] + list(range(0x2000,0x200b)) + [0x2028,0x2029,0x202f,0x205f,0x3000]
    assert len(whitespace) == 29
    for cp in whitespace:
        separator = chr(cp)
        s.sql(separator.join(["SELECT","id","FROM","t","WHERE","n","=","+","１２","ORDER","BY","id","LIMIT","١"]), rows=[[1]])
        s.sql("SELECT s FROM t WHERE s='１２٣\u00a0Ä'", rows=[["１２٣\u00a0Ä"]])
    for cp in [0x180e,0x200b,0x200c,0x200d,0x2060,0xfeff]:
        s.sql("SELECT"+chr(cp)+"id FROM t", fail=True)
    for zero in [0x30,0x660,0x6f0,0x966,0x9e6,0xe50,0xff10,0x104a0,0x1d7d8,0x11f50,0x1e4f0]:
        def digits(text):
            return "".join(chr(zero+ord(c)-48) if "0"<=c<="9" else c for c in text)
        s.sql("SELECT id FROM t WHERE n="+digits("12")+" LIMIT "+digits("1"), rows=[[1]])
        s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,["+digits("1.5e+1,.5")+"]) LIMIT "+digits("1"), rows=[[1]])
        s.sql("SELECT id FROM t WHERE id>="+digits("-9223372036854775808")+" AND id<="+digits("9223372036854775807"), rows=[[1]])
        s.sql("SELECT id FROM t LIMIT "+digits("18446744073709551616"), rows=[[1]])
        s.sql("INSERT INTO t VALUES(2,"+digits("9223372036854775808")+",'x',[0,0])", fail=True)
        s.sql("SELECT id FROM t LIMIT "+digits("-1"), fail=True)
        s.sql("UPDATE t SET n="+digits("12")+" WHERE id="+digits("1"), affected=1)
    s.sql("SELECT id FROM t WHERE n=1２ LIMIT ٠١", rows=[[1]])
    for value in ["²","①","Ⅳ","四","½","1٫2","１．２","1Ｅ2","−1","＋1","١e+",".e١"]:
        s.sql("UPDATE t SET n="+value+" WHERE id=1", fail=True)
    for literal in ["'[١,٢]'", "'[0,\u00a00]'", "'[0,１２]'", "'[.5,0]'"]:
        s.sql("UPDATE t SET v="+literal+" WHERE id=1", fail=True)
    s.sql("SELECT id١ FROM t", fail=True)
    s.sql("SELECT id FROM t -- comment\u2028DELETE FROM t", rows=[[1]])
    s.sql("SELECT n,s,v FROM t", rows=[[12,"１２٣\u00a0Ä",[15.0,0.5]]])
    s.stop()
    s = target.start()
    s.sql("SELECT id FROM t WHERE n=１２ LIMIT ١", rows=[[1]])
    s.sql("DELETE FROM t WHERE id=١", affected=1)
    s.stop()


CASES = [("crud_and_int64", crud_and_int64), ("type_errors_atomicity", type_errors_atomicity),
         ("invalid_sql_and_schema", invalid_sql_and_schema), ("schema_permutations", schema_permutations),
         ("float32_and_sorting", float32_and_sorting), ("transaction_combinations", transaction_combinations),
         ("shutdown_rolls_back", lambda target: recovery(target, "shutdown_uncommitted")),
         ("kill_uncommitted", lambda target: recovery(target, "uncommitted_kill")),
         ("kill_after_commit", lambda target: recovery(target, "commit_kill")),
         ("missing_index_rebuild", missing_index_rebuild), ("protocol_boundaries", protocol_boundaries),
         ("long_input_recovery", long_input_recovery), ("randomized_index_truth", randomized_index_truth),
         ("wide_records_fallback", wide_records_fallback),
         ("contiguous_cache_and_filters", contiguous_cache_and_filters),
         ("distance_reuse_and_residuals", distance_reuse_and_residuals),
         ("duplicate_alias_ordering", duplicate_alias_ordering),
         ("unicode_identifiers", unicode_identifiers), ("unicode_lexing", unicode_lexing)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker-image")
    parser.add_argument("--profile", action="store_true", help="enable stderr phase diagnostics in the target Docker container")
    parser.add_argument("--screen-block", type=int, choices=[0, 32, 64, 128], help="experimental distance-screen block size in Docker")
    parser.add_argument("--command", nargs=argparse.REMAINDER, help="native command; put last and include {data}")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--long-string-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--case", action="append", choices=[name for name, _ in CASES])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if bool(args.docker_image) == bool(args.command):
        parser.error("provide exactly one of --docker-image or --command")
    if args.profile and not args.docker_image:
        parser.error("--profile requires --docker-image")
    if args.screen_block is not None and not args.docker_image:
        parser.error("--screen-block requires --docker-image")
    if args.command and not any("{data}" in part for part in args.command):
        parser.error("--command must include {data} for independent persistent test directories")
    results = []
    for name, case in CASES:
        if args.case and name not in args.case:
            continue
        start = time.perf_counter()
        target = None
        result = {"case": name, "passed": False}
        try:
            target = Target(args)
            case(target)
            result["passed"] = True
        except Exception as exc:
            result.update(error=str(exc), traceback=traceback.format_exc())
            if target is not None:
                result["last_sql"] = target.last_sql
                result["last_request"] = target.last_request
                result["stderr"] = [list(session.errors) for session in target.sessions]
        finally:
            if target is not None:
                result["requests"] = target.requests
                try:
                    target.cleanup()
                except Exception as exc:
                    result.update(passed=False, cleanup_error=str(exc))
            result["seconds"] = time.perf_counter() - start
        results.append(result)
        print(("PASS " if result["passed"] else "FAIL ") + name + " (%.2fs)" % result["seconds"], flush=True)
        if "error" in result:
            print(result["error"][:2000], flush=True)
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Supplementary actual-process JSONL tests; no database Python imports; not official hidden tests or performance results",
              "target": args.docker_image or args.command, "profiling": args.profile, "screen_block": args.screen_block,
              "cases": len(results), "passed_cases": sum(r["passed"] for r in results),
              "requests": sum(r.get("requests", 0) for r in results), "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("%d/%d supplementary cases passed (%d JSONL requests)" % (report["passed_cases"], report["cases"], report["requests"]), flush=True)
    return 0 if report["passed_cases"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
