"""Black-box boundary and differential tests for the two submitted revisions.

This is a locally designed suite, not the organizer's hidden test suite.
Database implementations are never imported. Each group uses fresh Docker
volumes, the image's default entrypoint, and the existing JSONL transport.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from test_cpp import Target


def equal(a, b):
    if isinstance(b, dict):
        return isinstance(a, dict) and a.keys() == b.keys() and all(equal(a[k], v) for k, v in b.items())
    if isinstance(b, list):
        return isinstance(a, list) and len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if type(b) is float:
        return type(a) in (float, int) and math.isclose(a, b, abs_tol=1e-5, rel_tol=1e-5)
    return type(a) is type(b) and a == b


def compact(value):
    if isinstance(value, str) and len(value) > 2000:
        return {"prefix": value[:250], "characters": len(value),
                "sha256": hashlib.sha256(value.encode()).hexdigest()}
    if isinstance(value, int) and value.bit_length() > 1000:
        return {"integer_decimal_digits": len(str(value)), "sha256": hashlib.sha256(str(value).encode()).hexdigest()}
    if isinstance(value, list):
        if len(value) > 100:
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            return {"length": len(value), "first": compact(value[:3]), "last": compact(value[-3:]),
                    "sha256": hashlib.sha256(data).hexdigest()}
        return [compact(x) for x in value]
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items()}
    return value


def normal(response):
    if response.get("ok") is False:
        return {"ok": False}  # The published error-code mapping is empty.
    return {k: v for k, v in response.items() if k != "id"}


class Pair:
    def __init__(self, images, group, records, timeout):
        self.targets, self.sessions = {}, {}
        self.group, self.records, self.number = group, records, 0
        for label, image in images.items():
            args = SimpleNamespace(docker_image=image, command=None, profile=False,
                                   screen_block=None, timeout=timeout)
            target = Target(args)
            self.targets[label] = target
            self.sessions[label] = target.start()

    def step(self, name, sql=None, expected=None, *, mode="exact", raw=None,
             ident=None, scope="contract"):
        self.number += 1
        request_id = self.number if ident is None else ident
        entry = {"group": self.group, "name": name, "scope": scope,
                 "sql": compact(sql), "mode": mode, "expected": compact(expected),
                 "responses": {}, "failures": {}}
        responses = {}
        if raw is not None:
            entry["raw"] = compact(raw)
        for label, session in self.sessions.items():
            start = time.perf_counter()
            try:
                if raw is None:
                    response = session.request({"id": request_id, "op": "execute", "sql": sql,
                                                "search_mode": mode})
                else:
                    response = session.raw(raw)
                responses[label] = response
                entry["responses"][label] = compact(response)
                if expected is not None:
                    for key, value in expected.items():
                        if key not in response or not equal(response[key], value):
                            entry["failures"].setdefault(label, []).append("expected " + key)
            except Exception as exc:
                entry["failures"].setdefault(label, []).append(str(exc))
                responses[label] = {"transport_error": str(exc)}
                entry["responses"][label] = compact(responses[label])
            entry.setdefault("elapsed_ms", {})[label] = round((time.perf_counter() - start) * 1000, 3)
        entry["equivalent"] = equal(normal(responses["cpp"]), normal(responses["python"]))
        self.records.append(entry)
        if entry["failures"] or not entry["equivalent"]:
            print(json.dumps({"group": self.group, "name": name, "failures": compact(entry["failures"]),
                              "equivalent": entry["equivalent"]}, ensure_ascii=True), flush=True)
        return responses

    def restart(self, crash=False):
        for label, session in self.sessions.items():
            if crash:
                session.kill()
            else:
                session.stop()
            self.sessions[label] = self.targets[label].start()

    def close(self):
        errors = []
        for label, target in self.targets.items():
            try:
                if self.sessions[label].process.poll() is None:
                    self.sessions[label].stop()
            except Exception as exc:
                errors.append({"label": label, "shutdown_error": str(exc)})
            finally:
                target.cleanup()
        return errors


def ok(**values):
    return {"ok": True, **values}


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def numeric(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(4),v VECTOR(2))", ok())
    values = [-(2**63), -(2**53)-1, -1, 0, 1, 2**53, 2**53+1, 2**63-2, 2**63-1]
    p.step("signed-boundaries", "INSERT INTO t VALUES " + ",".join(f"({n},{n},'ok',[0,0])" for n in values), ok(affected_rows=len(values)))
    for boundary in values:
        for op, fn in [("=", lambda a, b: a == b), ("<", lambda a, b: a < b),
                       ("<=", lambda a, b: a <= b), (">", lambda a, b: a > b), (">=", lambda a, b: a >= b)]:
            p.step(f"integer-{boundary}-{op}", f"SELECT id FROM t WHERE n {op} {boundary} ORDER BY id",
                   ok(columns=["id"], rows=[[n] for n in values if fn(n, boundary)]))
    for literal in [str(-(2**63)-1), str(2**63), str(2**64-1), str(2**64), "9"*500,
                    "1.0", "1e0", "'1'", "NULL", "NaN", "Infinity", "1e309", "true"]:
        p.step("reject-bigint-" + literal[:30], f"INSERT INTO t VALUES ({literal},0,'x',[0,0])", {"ok": False})
    for literal in ["-0", "+2", "0003", "+0004"]:
        n = int(literal)
        p.step("integer-spelling-" + literal, f"SELECT id FROM t WHERE id={literal} ORDER BY id",
               ok(columns=["id"], rows=[[n]] if n in values else []))
    for limit in [0, 1, 9, 10, 4096, 4097, 2**63, 2**64, 10**100]:
        p.step("limit-" + str(limit)[:25], f"SELECT id FROM t ORDER BY id LIMIT {limit}",
               ok(columns=["id"], rows=[[n] for n in values[:limit]]))
    for literal in ["-1", "1.0", "1e0", "'1'", "NULL", "NaN"]:
        p.step("bad-limit-" + literal, f"SELECT id FROM t LIMIT {literal}", {"ok": False})
    p.step("atomic-before", "BEGIN", ok())
    p.step("prior-success", "INSERT INTO t VALUES (20,20,'yes',[1,2])", ok(affected_rows=1))
    p.step("atomic-batch-overflow", "INSERT INTO t VALUES (21,0,'yes',[0,0]),(22,9223372036854775808,'no',[0,0])", {"ok": False})
    p.step("commit-prior-success", "COMMIT", ok())
    p.restart()
    p.step("restart-exact-values", "SELECT id,n FROM t ORDER BY id", ok(columns=["id", "n"], rows=[[n,n] for n in sorted(values+[20])]))


def strings(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(4))", ok())
    texts = ["", "a", "A", "a ", " a", "中", "中文测验", "😀😁😂😃", "e\u0301", "a\x00b", "x\ny", "O'Re", "\\n", "\t", "é", "𝄞"]
    p.step("unicode-control-strings", "INSERT INTO t VALUES " + ",".join(f"({i},{quote(s)})" for i,s in enumerate(texts)), ok(affected_rows=len(texts)))
    for i,s in enumerate(texts):
        p.step(f"string-equality-{i}", f"SELECT id,s FROM t WHERE s={quote(s)} ORDER BY id", ok(columns=["id","s"], rows=[[i,s]]))
    for descending in (False, True):
        # Distinct strings: no dependency on incidental row order.
        order = sorted(range(len(texts)), key=lambda i: texts[i], reverse=descending)
        p.step("binary-unicode-sort-" + str(descending), "SELECT id FROM t ORDER BY s " + ("DESC" if descending else "ASC"), ok(columns=["id"], rows=[[i] for i in order]))
    for s in ["abcde", "中文测试长", "😀😁😂😃😄", "e\u0301e\u0301e"]:
        p.step("reject-varchar-overflow", f"INSERT INTO t VALUES (100,{quote(s)})", {"ok": False})
        p.step("reject-update-overflow", f"UPDATE t SET s={quote(s)} WHERE id=0", {"ok": False})
    p.step("overlong-filter-allowed", "SELECT id FROM t WHERE s='abcde'", ok(columns=["id"], rows=[]))
    p.restart()
    p.step("unicode-restart", "SELECT id,s FROM t ORDER BY id", ok(columns=["id","s"], rows=[[i,s] for i,s in enumerate(texts)]))


def sql_edges(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(8),v VECTOR(2))", ok())
    p.step("insert", "INSERT INTO t VALUES (3,2,'b',[3,4]),(1,2,'a',[0,0]),(2,1,'a',[-3,-4])", ok(affected_rows=3))
    for limit in [0,1,2,3,4,4097]:
        for desc in [False,True]:
            expected = [[1],[3],[2]] if desc else [[2],[1],[3]]
            p.step("scalar-ties", f"SELECT id FROM t ORDER BY n {'DESC' if desc else 'ASC'} LIMIT {limit}", ok(columns=["id"], rows=expected[:limit]))
    cases = [
        ("aliases", "SELECT id AS x,n AS y FROM t ORDER BY y DESC", [[1,2],[3,2],[2,1]], ["x","y"]),
        ("alias-shadows-column", "SELECT id,n AS id FROM t ORDER BY id", [[2,1],[1,2],[3,2]], ["id","id"]),
        ("repeated-projections", "SELECT id,id,n,n FROM t ORDER BY id", [[1,1,2,2],[2,2,1,1],[3,3,2,2]], ["id","id","n","n"]),
        ("comment-and-quote", "/* x */ SELECT `id` FROM t -- inline\n WHERE s='a' ORDER BY id;", [[1],[2]], ["id"]),
        ("empty-result", "SELECT id FROM t WHERE id>3 ORDER BY id LIMIT 10", [], ["id"]),
    ]
    for name, sql, rows, columns in cases:
        p.step(name, sql, ok(columns=columns, rows=rows))
    for sql in ["SELECT missing FROM t LIMIT 0", "SELECT id FROM t ORDER BY missing LIMIT 0",
                "SELECT id FROM t WHERE missing=1 LIMIT 0", "SELECT v FROM t ORDER BY v LIMIT 0",
                "SELECT id FROM t ORDER BY L2_DISTANCE(v,[1]) LIMIT 0", "SELECT id FROM t;;",
                "SELECT id FROM t; DELETE FROM t", "INSERT INTO t(id,n,s,v,id) VALUES(8,1,'a',[0,0],8)",
                "INSERT INTO t(id,n,s) VALUES(8,1,'a')", "UPDATE t SET n=1,n=2 WHERE id=1",
                "UPDATE t SET id=9 WHERE id=1", "ALTER TABLE t ADD a BIGINT"]:
        p.step("invalid-sql", sql, {"ok": False})
    p.step("unchanged-after-errors", "SELECT id,n FROM t ORDER BY id", ok(columns=["id","n"], rows=[[1,2],[2,1],[3,2]]))
    # These are compatibility probes, not assumed requirements of the subset.
    for sql in ["SELECT id FROM t WHERE id != 1", "SELECT id FROM t WHERE id <> 1",
                "SELECT id FROM t WHERE id=1 OR id=2", "SELECT COUNT(*) FROM t",
                "SELECT id FROM t ORDER BY id LIMIT 1 OFFSET 1", "SELECT id FROM t ORDER BY id LIMIT 1,1",
                "SELECT id FROM t WHERE n=2.0", "SELECT id FROM t WHERE n='2'",
                "SELECT id FROM t WHERE s='A' ORDER BY id", "UPDATE t SET n=2 WHERE id=1",
                "SELECT\u00a0id FROM t ORDER BY id", "SELECT id FROM t WHERE id=١",
                "CREATE TABLE `Ä` (`ID` BIGINT PRIMARY KEY)", "INSERT INTO `ä` VALUES(1)",
                "SELECT `id` FROM `Ä`"]:
        p.step("mysql-or-python-compatibility", sql, scope="compatibility")


def vector_edges(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(2))", ok())
    vectors = [[0,0],[-0.0,0.0],[3,4],[-3,4],[1e-45,0],[1e-38,1e-38],
               [16777217,16777216],[3.4028234663852886e38,0],[-3.4028234663852886e38,0]]
    data = [[i, i%3, [f32(x) for x in v]] for i,v in enumerate(vectors)]
    p.step("float32-extremes", "INSERT INTO t VALUES " + ",".join(f"({i},{i%3},{quote(json.dumps(v))})" for i,v in enumerate(vectors)), ok(affected_rows=len(vectors)))
    p.step("stored-float32", "SELECT id,v FROM t ORDER BY id", ok(columns=["id","v"], rows=[[i,v] for i,_,v in data]))
    for invalid in ["[]", "[0]", "[0,0,0]", "[NaN,0]", "[Infinity,0]", "[-Infinity,0]",
                    "[1e39,0]", "[1e309,0]", "[true,0]", "[null,0]", '["1",0]', '[[0],0]']:
        for literal in [invalid, quote(invalid)]:
            p.step("invalid-vector-insert", f"INSERT INTO t VALUES(50,0,{literal})", {"ok": False})
            p.step("invalid-vector-query", f"SELECT id FROM t ORDER BY L2_DISTANCE(v,{literal}) LIMIT 2", {"ok": False})
    for indexed in [False, True]:
        if indexed:
            p.step("index", "CREATE VECTOR INDEX ix ON t(v)", ok())
        for query in [[0,0],[3,4],[1e-45,0],[3.4028234663852886e38,0]]:
            query = [f32(x) for x in query]
            literal = quote(json.dumps(query))
            dist = {i: math.sqrt(math.fsum((a-b)**2 for a,b in zip(v,query))) for i,_,v in data}
            for filtered in [False, True]:
                selected = [i for i,tag,_ in data if not filtered or tag==0]
                for desc in [False, True]:
                    ranked = sorted(selected, key=lambda i: (-dist[i] if desc else dist[i],i))
                    for limit in [0,1,3,30,4097]:
                        sql = f"SELECT id,L2_DISTANCE(v,{literal}) AS d FROM t " + ("WHERE tag=0 " if filtered else "") + f"ORDER BY d {'DESC' if desc else 'ASC'} LIMIT {limit}"
                        p.step("extreme-topk", sql, ok(columns=["id","d"], rows=[[i,dist[i]] for i in ranked[:limit]]), mode="ann" if indexed else "exact")
    p.restart()
    p.step("index-restart", "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 2", ok(columns=["id"], rows=[[0],[1]]), mode="ann")


def random_state(p):
    rng = random.Random(20261113)
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(8),v VECTOR(5))", ok())
    model, saved = {}, None
    def snapshot():
        p.step("state-oracle", "SELECT id,n,s,v FROM t ORDER BY id", ok(columns=["id","n","s","v"], rows=[[i,*model[i]] for i in sorted(model)]))
    for step in range(160):
        action = rng.choice(["insert","insert","update","delete","query","begin","end","restart"])
        if action == "insert":
            i = rng.randrange(-15,30)
            value = [rng.randint(-100,100), rng.choice(["","a","中","😀","a "]), [f32(rng.uniform(-3,3)) for _ in range(5)]]
            p.step("random-insert", f"INSERT INTO t VALUES({i},{value[0]},{quote(value[1])},{quote(json.dumps(value[2]))})", {"ok":False} if i in model else ok(affected_rows=1))
            if i not in model:
                model[i] = value
        elif action == "update":
            n, threshold = rng.randrange(-20,20), rng.randrange(-15,30)
            chosen = [i for i in model if i>=threshold]
            p.step("random-update", f"UPDATE t SET n={n} WHERE id>={threshold}", ok(affected_rows=len(chosen)))
            for i in chosen:
                model[i] = [n,*model[i][1:]]
        elif action == "delete":
            threshold = rng.randrange(-15,30)
            chosen = [i for i in model if i<threshold]
            p.step("random-delete", f"DELETE FROM t WHERE id<{threshold}", ok(affected_rows=len(chosen)))
            for i in chosen:
                del model[i]
        elif action == "begin":
            p.step("random-begin", "BEGIN", ok() if saved is None else {"ok":False})
            if saved is None:
                saved = dict(model)
        elif action == "end":
            command = rng.choice(["COMMIT","ROLLBACK"])
            p.step("random-end", command, ok() if saved is not None else {"ok":False})
            if saved is not None:
                if command=="ROLLBACK":
                    model = saved
                saved = None
        elif action == "restart":
            p.restart(crash=bool(step%2))
            if saved is not None:
                model, saved = saved, None
        else:
            query = [f32(rng.uniform(-3,3)) for _ in range(5)]
            selected = [i for i in model if model[i][0]>=-10]
            dist = {i:math.sqrt(math.fsum((a-b)**2 for a,b in zip(model[i][2],query))) for i in selected}
            ranked = sorted(selected,key=lambda i:(dist[i],i))[:10]
            p.step("random-vector-oracle", f"SELECT id FROM t WHERE n>=-10 ORDER BY L2_DISTANCE(v,{quote(json.dumps(query))}) LIMIT 10", ok(columns=["id"],rows=[[i] for i in ranked]))
        if step==50:
            p.step("random-create-index", "CREATE VECTOR INDEX ix ON t(v)", ok())
        snapshot()


def wide(p):
    # Actual strings exceed the 16 MiB cache budget; sort runs exceed 2 MiB.
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(24000),v VECTOR(3))", ok())
    for batch in range(16):
        rows = []
        for i in range(batch*50, (batch+1)*50):
            rows.append(f"({i},{quote(str(i%7)+'x'*23999)},[{i%7},0,0])")
        p.step("wide-insert-"+str(batch), "INSERT INTO t VALUES "+",".join(rows), ok(affected_rows=50))
    for desc in [False, True]:
        order = sorted(range(800),key=lambda i:(-(i%7) if desc else i%7,i))
        p.step("wide-external-sort", f"SELECT id FROM t ORDER BY s {'DESC' if desc else 'ASC'}", ok(columns=["id"],rows=[[i] for i in order]))
        p.step("wide-topk", f"SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0,0]) {'DESC' if desc else 'ASC'} LIMIT 10", ok(columns=["id"],rows=[[i] for i in order[:10]]))
    p.step("response-spill", "SELECT id,s FROM t ORDER BY id LIMIT 32", ok(columns=["id","s"],rows=[[i,str(i%7)+'x'*23999] for i in range(32)]))
    p.step("wide-index", "CREATE VECTOR INDEX ix ON t(v)", ok())
    p.restart(crash=True)
    p.step("wide-index-filter", "SELECT id FROM t WHERE id>=700 ORDER BY L2_DISTANCE(v,[0,0,0]) LIMIT 10", ok(columns=["id"],rows=[[i] for i in sorted(range(700,800),key=lambda i:(i%7,i))[:10]]), mode="ann")


def protocol(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY)", ok())
    for ident in ["001", 2**53+1, 2**63-1, 2**64, 10**200, 1.25, True, [1,"中"], {"x":1}]:
        p.step("id-roundtrip", "SELECT id FROM t", ok(columns=["id"],rows=[]), ident=ident)
    for raw in [
        '{"id":null,"op":"execute","sql":"SELECT id FROM t"}',
        '{"id":1.0,"op":"execute","sql":"SELECT id FROM t"}',
        '{"id":-0,"op":"execute","sql":"SELECT id FROM t"}',
    ]:
        expected_id = json.loads(raw)["id"]
        p.step("id-raw-types", raw=raw, expected=ok(id=expected_id, columns=["id"],rows=[]))
    malformed = ['{"id":1,"id":2,"op":"execute"}', '{"id":1,"op":"execute","sql":2}',
                 '{"id":1,"op":"execute","sql":"SELECT id FROM t","search_mode":null}',
                 '{"id":1,"op":"execute","sql":"SELECT id FROM t","search_mode":[]}',
                 '{"id":1,"op":"wrong"}', '[]', 'null', '{',
                 '{"id":1,"op":"execute","sql":"SELECT id FROM t","x":NaN}',
                 '{"id":1,"op":"execute","sql":"SELECT id FROM t","x":1e400}']
    for raw in malformed:
        p.step("invalid-json-request", raw=raw, expected={"ok":False})
        p.step("live-after-invalid", "SELECT id FROM t", ok(columns=["id"],rows=[]))
    giant_id = int("7"*5000)
    raw = '{"id":'+str(giant_id)+',"op":"execute","sql":"SELECT id FROM t"}'
    p.step("5000-digit-id", raw=raw, expected=ok(id=giant_id,columns=["id"],rows=[]))
    p.step("live-after-giant-id", "SELECT id FROM t", ok(columns=["id"],rows=[]))


def unicode_identifiers(p):
    p.step("schema", "CREATE TABLE u(id BIGINT PRIMARY KEY,`Ä` BIGINT)", ok())
    p.step("insert", "INSERT INTO u VALUES(1,7)", ok(affected_rows=1))
    # Keep the table name ASCII: MySQL table-name folding depends on platform,
    # whereas these probes isolate column/alias folding in the two programs.
    for sql in ["SELECT `Ä` FROM u", "SELECT `ä` AS x FROM u",
                "SELECT id FROM u WHERE `ä`=7", "SELECT id FROM u ORDER BY `ä`",
                "SELECT id AS `Ö` FROM u ORDER BY `ö`", "SELECT id AS `Ö` FROM u"]:
        p.step("unicode-column-or-alias", sql, scope="compatibility")


GROUPS = {"numeric":numeric,"strings":strings,"sql":sql_edges,"vector":vector_edges,
          "random_state":random_state,"wide":wide,"protocol":protocol,
          "unicode_identifiers":unicode_identifiers}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-image", default="minivector-boundary:python-f1b9f4e")
    parser.add_argument("--cpp-image", default="minivector-boundary:cpp-8c2bbcac8")
    parser.add_argument("--group", action="append", choices=GROUPS)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    images = {"python":args.python_image,"cpp":args.cpp_image}
    records, groups = [], []
    for name in args.group or GROUPS:
        print("START "+name, flush=True)
        pair = None
        start, before = time.perf_counter(), len(records)
        outcome = {"name":name}
        try:
            pair = Pair(images,name,records,args.timeout)
            GROUPS[name](pair)
        except Exception as exc:
            outcome["infrastructure_error"] = str(exc)
        finally:
            if pair:
                outcome["cleanup_errors"] = pair.close()
        outcome.update(steps=len(records)-before,elapsed_seconds=round(time.perf_counter()-start,3))
        groups.append(outcome)
        (args.output/"steps.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
        print("DONE "+json.dumps(outcome), flush=True)
    defects = [r for r in records if r["failures"]]
    differences = [r for r in records if not r["equivalent"]]
    report = {"timestamp_utc":datetime.now(timezone.utc).isoformat(),
              "scope":"Locally designed boundary tests, not official hidden cases or official pass evidence",
              "images":{label:json.loads(subprocess.check_output(["docker","inspect",image]))[0]["Id"] for label,image in images.items()},
              "groups":groups,"steps":len(records),"requests_excluding_lifecycle":len(records)*2,
              "expectation_failures":{label:sum(label in r["failures"] for r in records) for label in images},
              "differential_count":len(differences),
              "differential_by_scope":dict(Counter(r["scope"] for r in differences)),
              "failures":defects,"differences":differences,
              "limits":["No official hidden inputs or formal platform logs available.",
                        "Error code text is recorded but not equated; the public mapping is empty.",
                        "Wide case exceeds cache/sort budgets, not the full 2 GiB process memory limit.",
                        "This suite measures correctness; per-request timings are diagnostics, not performance scores."]}
    (args.output/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:report[k] for k in ["steps","expectation_failures","differential_count","differential_by_scope"]}),flush=True)
    return int(any("infrastructure_error" in g or g.get("cleanup_errors") for g in groups) or bool(defects))


if __name__ == "__main__":
    raise SystemExit(main())
