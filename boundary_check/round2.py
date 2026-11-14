"""Additional MySQL-inspired black-box probes; not the organizer's hidden suite.

No database implementation is imported. Contract expectations use a separate
small row model. MySQL-only syntax is recorded as compatibility, not required.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import subprocess
import time

from run import Pair, compact, equal, f32, ok, quote

SOURCES = [
    "https://github.com/mysql/mysql-server/blob/8.4/mysql-test/t/comments.test",
    "https://github.com/mysql/mysql-server/blob/8.4/mysql-test/t/limit.test",
    "https://github.com/mysql/mysql-server/blob/8.4/mysql-test/t/alias.test",
    "https://dev.mysql.com/doc/refman/8.4/en/string-literals.html",
    "https://dev.mysql.com/doc/refman/8.4/en/problems-with-alias.html",
    "https://dev.mysql.com/doc/refman/8.4/en/innodb-error-handling.html",
]


def result(p, name, sql, columns, rows, **kwargs):
    return p.step(name, sql, ok(columns=columns, rows=rows), **kwargs)


def compatibility(p, name, sql, mysql_expected=None):
    response = p.step(name, sql, scope="mysql_compatibility")
    p.records[-1]["mysql_expected"] = mysql_expected
    if mysql_expected is not None:
        p.records[-1]["mysql_mismatch"] = {
            label: any(not equal(r.get(k), v) for k, v in mysql_expected.items())
            for label, r in response.items()
        }
    return response


def lexical(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(64),v VECTOR(2))", ok())
    p.step("seed", "INSERT INTO t VALUES(1,7,'a',[0,0]),(2,3,'b',[1,0])", ok(affected_rows=2))
    tokens = ["SELECT", "id", "FROM", "t", "WHERE", "n", ">=", "3", "ORDER", "BY", "id", "LIMIT", "2"]
    for separator in [" ", "\t", "\r", "\n", "\r\n", "\v", "\f", "/**/", "/* ' ` ; -- */", "-- comment\n"]:
        result(p, "token-separator-" + repr(separator), separator.join(tokens), ["id"], [[1], [2]])
    for suffix in [";", "; -- tail", "; /*tail*/", "-- tail", " /*two\nlines*/;"]:
        result(p, "trailing-comment-" + repr(suffix), "SELECT id FROM t ORDER BY id" + suffix, ["id"], [[1], [2]])
    strings = ["'", "''", "a'b", "--x", "/*x*/", "; DELETE FROM t;", "`quoted`", "#x", "a\r\nb", "\\", "x\\'y"]
    for i, value in enumerate(strings, 10):
        p.step("quoted-punctuation-insert", f"INSERT INTO t VALUES({i},0,{quote(value)},[0,0])", ok(affected_rows=1))
        result(p, "quoted-punctuation-read", f"SELECT s FROM t WHERE id={i}", ["s"], [[value]])
    for sql in [
        "SELECT id FROM t; DELETE FROM t", "SELECT id FROM t;;", "SELECT * AS x FROM t",
        "SELECT id FROM t /*", "SELECT id FROM t */", "SELECT id FROM t WHERE n=7 AND",
        "UPDATE t SET n=9 WHERE id=1 trailing", "DELETE FROM t WHERE id=1 trailing",
        "INSERT INTO t VALUES(99,1,'x',[0,0]); SELECT id FROM t", "SELECT id+1 FROM t",
        "SELECT id FROM t WHERE id=1 OR id=2", "SELECT id FROM t LIMIT 1 ORDER BY id",
        "UPDATE t SET n=9,missing=1 WHERE id=1", "SELECT 'id' FROM t",
    ]:
        p.step("reject-whole-statement", sql, {"ok": False})
    result(p, "failed-statements-preserve-row", "SELECT id,n FROM t WHERE id<=2 ORDER BY id", ["id", "n"], [[1,7], [2,3]])
    probes = [
        ("hash-comment", "SELECT id FROM t WHERE id=1 # comment", ok(columns=["id"],rows=[[1]])),
        ("dash-without-space", "SELECT id FROM t WHERE id=1--x", {"ok":False}),
        ("version-comment", "SELECT id FROM t /*!80000 WHERE id=1 */ ORDER BY id", ok(columns=["id"],rows=[[1]])),
        ("adjacent-literals", "SELECT id FROM t WHERE s='a' ''", ok(columns=["id"],rows=[[1]])),
        ("double-quoted-literal", 'SELECT id FROM t WHERE s="a"', ok(columns=["id"],rows=[[1]])),
        ("national-literal", "SELECT id FROM t WHERE s=N'a'", ok(columns=["id"],rows=[[1]])),
        ("charset-literal", "SELECT id FROM t WHERE s=_utf8mb4'a'", ok(columns=["id"],rows=[[1]])),
        ("string-quoted-alias", "SELECT id AS 'x' FROM t WHERE id=1", ok(columns=["x"],rows=[[1]])),
        ("implicit-alias", "SELECT id x FROM t WHERE id=1", ok(columns=["x"],rows=[[1]])),
        ("ordinal-order", "SELECT n FROM t WHERE id<=2 ORDER BY 1", ok(columns=["n"],rows=[[3],[7]])),
        ("limit-offset", "SELECT id FROM t ORDER BY id LIMIT 1 OFFSET 1", ok(columns=["id"],rows=[[2]])),
        ("limit-comma", "SELECT id FROM t ORDER BY id LIMIT 1,1", ok(columns=["id"],rows=[[2]])),
        ("not-equal", "SELECT id FROM t WHERE id<>1 AND id<=2", ok(columns=["id"],rows=[[2]])),
        ("not-equal-bang", "SELECT id FROM t WHERE id!=1 AND id<=2", ok(columns=["id"],rows=[[2]])),
    ]
    for name, sql, expected in probes:
        compatibility(p, name, sql, expected)
    # Probe MySQL's default escape processing separately from JSON escaping.
    for i, (literal, decoded) in enumerate([(r"'a\nb'", "a\nb"), (r"'a\tb'", "a\tb"), (r"'a\0b'", "a\x00b"),
                                             (r"'a\qb'", "aqb"), (r"'a\'b'", "a'b"), (r"'a\\b'", "a\\b")], 100):
        compatibility(p, "mysql-backslash-insert", f"INSERT INTO t VALUES({i},0,{literal},[0,0])", ok(affected_rows=1))
        compatibility(p, "mysql-backslash-value", f"SELECT s FROM t WHERE id={i}", ok(columns=["s"],rows=[[decoded]]))
    compatibility(p, "noop-update-count", "UPDATE t SET n=7 WHERE id=1", ok(affected_rows=0))


def eager_validation(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(4),v VECTOR(2))", ok())
    invalid = [
        "SELECT missing FROM t{where}{limit}",
        "SELECT L2_DISTANCE(n,[0,0]) FROM t{where}{limit}",
        "SELECT L2_DISTANCE(v,[0]) FROM t{where}{limit}",
        "SELECT L2_DISTANCE(v,[1e309,0]) FROM t{where}{limit}",
        "SELECT L2_DISTANCE(v,'[true,0]') FROM t{where}{limit}",
        "SELECT id FROM t{where} ORDER BY missing{limit}",
        "SELECT id FROM t{where} ORDER BY v{limit}",
        "SELECT v AS x FROM t{where} ORDER BY x{limit}",
        "SELECT id FROM t{where} ORDER BY L2_DISTANCE(v,[0]){limit}",
    ]
    predicates = ["missing=1", "n='1'", "n=9223372036854775808", "s=1", "v='[0,0]'", "n=1.0"]
    for state in ["empty", "filled", "indexed", "deleted"]:
        if state == "filled":
            p.step("seed", "INSERT INTO t VALUES(1,1,'x',[0,0])", ok(affected_rows=1))
        elif state == "indexed":
            p.step("index", "CREATE VECTOR INDEX vi ON t(v)", ok())
        elif state == "deleted":
            p.step("delete-all", "DELETE FROM t", ok(affected_rows=1))
        for where in ["", " WHERE id=-999"]:
            for limit in ["", " LIMIT 0"]:
                for template in invalid:
                    p.step(state + "-invalid-expression", template.format(where=where, limit=limit), {"ok":False})
        for condition in predicates:
            for prefix in ["", "id=-999 AND "]:
                p.step(state + "-invalid-predicate", "SELECT id FROM t WHERE " + prefix + condition + " LIMIT 0", {"ok":False})
        for sql in ["UPDATE t SET s='abcde'", "UPDATE t SET v=[0]", "UPDATE t SET missing=1", "UPDATE t SET id=2",
                    "UPDATE t SET n=2,n=3", "DELETE FROM t WHERE n='bad'", "UPDATE t SET n=2 WHERE missing=1"]:
            p.step(state + "-invalid-mutation", sql, {"ok":False})
        rows = [[1,1,"x",[0.0,0.0]]] if state in ("filled", "indexed") else []
        result(p, state + "-no-side-effects", "SELECT * FROM t ORDER BY id", ["id","n","s","v"], rows)


def aliases(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(8),v VECTOR(2))", ok())
    p.step("seed", "INSERT INTO t VALUES(1,30,'b',[0,0]),(2,10,'a',[10,0]),(3,20,'a',[5,0])", ok(affected_rows=3))
    for indexed in [False, True]:
        if indexed:
            p.step("index", "CREATE VECTOR INDEX vi ON t(v)", ok())
        result(p, "alias-shadows-column", "SELECT id AS n FROM t ORDER BY n", ["n"], [[1],[2],[3]])
        result(p, "where-uses-source-column", "SELECT id AS n FROM t WHERE n>=20 ORDER BY n DESC", ["n"], [[3],[1]])
        p.step("where-not-output-alias", "SELECT id AS q FROM t WHERE q=1", {"ok":False})
        result(p, "order-unprojected-column", "SELECT id FROM t ORDER BY n DESC LIMIT 2", ["id"], [[1],[3]])
        result(p, "distance-alias-shadows-scalar", "SELECT id,L2_DISTANCE(v,[10,0]) AS n FROM t ORDER BY n LIMIT 2", ["id","n"], [[2,0.0],[3,5.0]])
        result(p, "scalar-alias-shadows-vector", "SELECT id AS v FROM t ORDER BY v DESC", ["v"], [[3],[2],[1]])
        result(p, "duplicate-projections", "SELECT id,id AS q,n,n FROM t WHERE id=1", ["id","q","n","n"], [[1,1,30,30]])
        result(p, "quoted-identifier-escaping", "SELECT id AS `a``b` FROM t ORDER BY `a``b` DESC LIMIT 1", ["a`b"], [[3]])
        result(p, "keyword-alias", "SELECT id AS `order` FROM t ORDER BY `order` DESC LIMIT 1", ["order"], [[3]])
        for sql in [
            "SELECT id AS x,n AS x FROM t ORDER BY x LIMIT 1",
            "SELECT id,L2_DISTANCE(v,[0,0]) AS x,L2_DISTANCE(v,[10,0]) AS x FROM t ORDER BY x LIMIT 1",
            "SELECT id,L2_DISTANCE(v,[0,0]) AS x,n AS x FROM t ORDER BY x LIMIT 1",
            "SELECT id,n AS x,L2_DISTANCE(v,[10,0]) AS x FROM t ORDER BY x LIMIT 1",
            "SELECT v AS x,n AS x FROM t ORDER BY x LIMIT 0",
            "SELECT n AS x,v AS x FROM t ORDER BY x LIMIT 0",
        ]:
            compatibility(p, "ambiguous-duplicate-alias-indexed-" + str(indexed), sql)


def distance(row, query):
    return math.sqrt(math.fsum((float(a)-float(b))*(float(a)-float(b)) for a,b in zip(row["v"], query)))


def topk(p, name, table, model, where, predicate, query, count=10, desc=False, mode="exact"):
    rounded = [f32(x) for x in query]
    chosen = sorted((r for r in model.values() if predicate(r)), key=lambda r: ((-1 if desc else 1)*distance(r,rounded), r["id"]))[:count]
    rows = [[r["id"], distance(r, rounded), r["tag"]] for r in chosen]
    sql = f"SELECT id,L2_DISTANCE(v,{json.dumps(query)}) AS d,tag FROM {table}"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY d" + (" DESC" if desc else " ASC") + f" LIMIT {count}"
    result(p, name, sql, ["id","d","tag"], rows, mode=mode)


def float_boundaries(p):
    p.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(17))", ok())
    rng = random.Random(2026111302)
    model = {}
    exponents = [-149, -126, -50, -1, 0, 20, 60, 100, 127]
    for i in range(192):
        values = [math.ldexp(rng.choice([-1.0, 0.0, 1.0, 1.5]), rng.choice(exponents)) for _ in range(17)]
        if i < 64:
            values = [1e20] + [rng.choice([-1e-6,0.0,1e-6]) for _ in range(16)]
        model[i-96] = {"id":i-96,"tag":str(i%4),"v":[f32(x) for x in values]}
    records = [f"({r['id']},{quote(r['tag'])},{json.dumps(r['v'])})" for r in model.values()]
    p.step("seed-mixed-exponents", "INSERT INTO t VALUES" + ",".join(records), ok(affected_rows=len(records)))
    queries = [[0.0]*17, [1e20]+[0.0]*16, [math.ldexp(1.0,-149)]*17, [math.ldexp(-1.0,127)]*17]
    for state in ["scan", "index", "restart"]:
        if state=="index":
            p.step("index", "CREATE VECTOR INDEX vi ON t(v)", ok())
        if state=="restart":
            p.restart(crash=True)
        for qi, query in enumerate(queries):
            for count in [0,1,10,193,4097]:
                for desc in [False,True]:
                    topk(p, state+"-mixed-exponent-"+str(qi), "t", model, "", lambda r:True, query, count, desc)
            for tag in ["0","3","none"]:
                topk(p, state+"-filtered-subnormal", "t", model, "tag="+quote(tag), lambda r,t=tag:r["tag"]==t, query, mode="ann")
    p.step("rounding-schema", "CREATE TABLE z(id BIGINT PRIMARY KEY,v VECTOR(1))", ok())
    max32 = f32(3.4028234663852886e38)
    half_overflow = max32 + 2.0**103
    values = [0.0,-0.0,2.0**-150,math.nextafter(2.0**-150,math.inf),math.nextafter(2.0**-150,0.0),
              2.0**-149,2.0**-126,1+2.0**-24,math.nextafter(1+2.0**-24,math.inf),
              max32,math.nextafter(half_overflow,0),half_overflow,-half_overflow]
    for i,value in enumerate(values):
        try:
            rounded = f32(value)
            expected = ok(affected_rows=1) if math.isfinite(rounded) else {"ok":False}
        except OverflowError:
            expected = {"ok":False}
            rounded = None
        p.step("float32-rounding-"+str(i), f"INSERT INTO z VALUES({i},[{value!r}])", expected)
        result(p, "float32-stored-"+str(i), f"SELECT v FROM z WHERE id={i}", ["v"], [[[rounded]]] if expected["ok"] else [])


def filters_and_state(p):
    rng = random.Random(2026111303)
    model = {i:{"id":i,"n":(i*17)%53,"tag":str(i%41),"v":[float(i%23),float(i%7),float(i%3)]} for i in range(-120,120)}
    for table, columns in [("t","id BIGINT PRIMARY KEY,n BIGINT,tag VARCHAR(12),v VECTOR(3)"),
                           ("u","v VECTOR(3),tag VARCHAR(12),n BIGINT,id BIGINT PRIMARY KEY")]:
        p.step("schema-"+table, f"CREATE TABLE {table}({columns})", ok())
        records = [f"({r['id']},{r['n']},{quote(r['tag'])},{json.dumps(r['v'])})" for r in model.values()]
        p.step("seed-"+table, f"INSERT INTO {table}(id,n,tag,v) VALUES"+",".join(records), ok(affected_rows=len(records)))
    p.step("indexed-twin", "CREATE VECTOR INDEX ui ON u(v)", ok())

    def verify(phase, count=55):
        # More than 32 equality keys forces posting-list replacement.
        for j in range(count):
            tag=str(j%43)
            num=rng.randrange(53)
            conds=[("tag="+quote(tag),lambda r,t=tag:r["tag"]==t),
                   (f"n>={num}",lambda r,n=num:r["n"]>=n),
                   ("id>=-100",lambda r:r["id"]>=-100)]
            rng.shuffle(conds)
            if j%7==0:
                conds += [("tag='impossible'",lambda r:r["tag"]=="impossible")]
            elif j%5==0:
                conds += [("tag="+quote(tag),lambda r,t=tag:r["tag"]==t)]
            where=" AND ".join(c for c,_ in conds)
            predicate=lambda r,conditions=conds:all(fn(r) for _,fn in conditions)
            query=[rng.uniform(-10,25),float(j%7),0.0]
            for table in ["t","u"]:
                topk(p, phase+"-"+table, table, model, where, predicate, query, count=[1,3,10,500][j%4], mode="ann" if j%2 else "exact")
        for table in ["t","u"]:
            result(p, phase+"-full-state-"+table, f"SELECT id,n,tag,v FROM {table} ORDER BY id", ["id","n","tag","v"],
                   [[r[k] for k in ["id","n","tag","v"]] for r in sorted(model.values(),key=lambda r:r["id"])])
    verify("warm-and-evict")
    saved={k:{**v,"v":v["v"][:]} for k,v in model.items()}
    for disposition in ["ROLLBACK", "COMMIT"]:
        p.step("begin", "BEGIN", ok())
        affected=sum(i<0 for i in model)
        for table in ["t","u"]:
            p.step("update-cached-filter", f"UPDATE {table} SET tag='changed',n=52,v=[-1,-2,-3] WHERE id<0", ok(affected_rows=affected))
            p.step("batch-duplicate-after-valid", f"INSERT INTO {table}(id,n,tag,v) VALUES(999,1,'x',[0,0,0]),(1,1,'x',[0,0,0])", {"ok":False})
            p.step("invalid-after-valid", f"UPDATE {table} SET n=1,v=[1] WHERE id>=0", {"ok":False})
        for i in list(model):
            if i<0: model[i]={"id":i,"n":52,"tag":"changed","v":[-1.0,-2.0,-3.0]}
        verify("read-own-writes-"+disposition,12)
        p.step(disposition, disposition, ok())
        if disposition=="ROLLBACK": model={k:{**v,"v":v["v"][:]} for k,v in saved.items()}
        p.restart(crash=True)
        verify("after-restart-"+disposition,42)
    for table in ["t","u"]:
        p.step("delete-all", f"DELETE FROM {table}", ok(affected_rows=len(model)))
    model={}
    verify("empty-after-delete",2)
    for table in ["t","u"]:
        p.step("reuse-primary-key", f"INSERT INTO {table}(id,n,tag,v) VALUES(-1,2,'new',[1,2,3])", ok(affected_rows=1))
    model={-1:{"id":-1,"n":2,"tag":"new","v":[1.0,2.0,3.0]}}
    p.restart(crash=True)
    verify("reinsert-after-empty",4)


def transactions(p):
    for table in ["a","b"]:
        p.step("schema", f"CREATE TABLE {table}(id BIGINT PRIMARY KEY,s VARCHAR(4),v VECTOR(2))", ok())
        p.step("seed", f"INSERT INTO {table} VALUES(1,'old',[0,0]),(2,'old',[1,0])", ok(affected_rows=2))
    for end in ["ROLLBACK","COMMIT"]:
        p.step("begin", "BEGIN", ok())
        p.step("change-a", "UPDATE a SET s='new' WHERE id=1", ok(affected_rows=1))
        p.step("change-b", "DELETE FROM b WHERE id=2", ok(affected_rows=1))
        for sql in ["BEGIN", "CREATE TABLE a(id BIGINT PRIMARY KEY)", "INSERT INTO a VALUES(3,'x',[0,0]),(1,'x',[0,0])",
                    "UPDATE b SET s='longer'", "CREATE VECTOR INDEX bad ON b(s)", "SELECT missing FROM a"]:
            p.step("failure-keeps-transaction", sql, {"ok":False})
        p.step("create-index-in-tx", "CREATE VECTOR INDEX ai ON a(v)", ok())
        result(p, "read-own-deletion", "SELECT id FROM b ORDER BY id", ["id"], [[1]])
        p.step(end, end, ok())
        p.restart(crash=True)
        result(p, "a-after-end", "SELECT id,s FROM a ORDER BY id", ["id","s"], [[1,"new" if end=="COMMIT" else "old"],[2,"old"]])
        result(p, "b-after-end", "SELECT id FROM b ORDER BY id", ["id"], [[1]] if end=="COMMIT" else [[1],[2]])
        if end=="ROLLBACK":
            p.step("index-name-reusable-after-rollback", "CREATE VECTOR INDEX ai ON a(v)", ok())
            # Use another name for the second transaction.
            p.step("index-creation-rollback-setup", "BEGIN", ok())
            p.step("index-creation-rollback-probe", "CREATE VECTOR INDEX ai2 ON a(v)", ok())
            p.step("index-creation-rollback-finish", "ROLLBACK", ok())
            # Duplicate index creation in the next transaction is expected to fail;
            # the loop below is intentionally separate to preserve its state.
            break
    p.step("begin-second", "BEGIN", ok())
    p.step("second-a", "UPDATE a SET s='new' WHERE id=1", ok(affected_rows=1))
    p.step("second-b", "DELETE FROM b WHERE id=2", ok(affected_rows=1))
    p.step("second-index", "CREATE VECTOR INDEX ai2 ON a(v)", ok())
    p.step("second-error", "INSERT INTO a VALUES(1,'x',[1,0])", {"ok":False})
    p.step("commit-prior-successes", "COMMIT", ok())
    p.restart(crash=True)
    result(p,"committed-a","SELECT id,s FROM a ORDER BY id",["id","s"],[[1,"new"],[2,"old"]])
    result(p,"committed-b","SELECT id FROM b ORDER BY id",["id"],[[1]])
    p.step("begin-ddl", "BEGIN", ok())
    p.step("create-transient-table", "CREATE TABLE transient(id BIGINT PRIMARY KEY,v VECTOR(2))", ok())
    p.step("insert-transient", "INSERT INTO transient VALUES(1,[0,0])", ok(affected_rows=1))
    p.step("index-transient", "CREATE VECTOR INDEX ti ON transient(v)", ok())
    p.restart(crash=True)
    p.step("uncommitted-ddl-not-visible", "SELECT * FROM transient", {"ok":False})
    p.step("recreate-after-recovery", "CREATE TABLE transient(id BIGINT PRIMARY KEY,v VECTOR(2))", ok())
    p.step("reuse-transient-index-name", "CREATE VECTOR INDEX ti ON transient(v)", ok())


GROUPS = {"lexical":lexical,"eager_validation":eager_validation,"aliases":aliases,
          "float_boundaries":float_boundaries,"filters_and_state":filters_and_state,"transactions":transactions}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--python-image",default="minivector-boundary:python-f1b9f4e")
    parser.add_argument("--cpp-image",default="minivector-boundary:cpp-8c2bbcac8")
    parser.add_argument("--group",action="append",choices=GROUPS)
    parser.add_argument("--timeout",type=float,default=60)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    images={"python":args.python_image,"cpp":args.cpp_image}
    records,groups=[],[]
    for name in args.group or GROUPS:
        print("START "+name,flush=True)
        p=None
        before,start=len(records),time.perf_counter()
        group={"name":name}
        try:
            p=Pair(images,name,records,args.timeout)
            GROUPS[name](p)
        except Exception as exc:
            group["infrastructure_error"]=repr(exc)
        finally:
            if p: group["cleanup_errors"]=p.close()
        group.update(steps=len(records)-before,seconds=round(time.perf_counter()-start,3))
        groups.append(group)
        (args.output/"steps.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
        print("DONE "+json.dumps(group),flush=True)
    report={"timestamp_utc":datetime.now(timezone.utc).isoformat(),"sources":SOURCES,"groups":groups,"image_tags":images,
            "script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "image_ids":{k:json.loads(subprocess.check_output(["docker","inspect",v]))[0]["Id"] for k,v in images.items()},
            "steps":len(records),"by_scope":dict(Counter(r["scope"] for r in records)),
            "failures":{label:sum(label in r["failures"] for r in records) for label in images},
            "differences":[r for r in records if not r["equivalent"]],
            "failed_steps":[r for r in records if r["failures"]],
            "mysql_compatibility_mismatches":[r for r in records if any(r.get("mysql_mismatch",{}).values())],
            "limitations":["Locally designed probes, not organizer hidden tests.","Full MySQL behavior is not the contest contract.",
                           "Default image entrypoints; 2 CPU, 2 GiB, network none. Timing is diagnostic only.",
                           "Ambiguous duplicate aliases are differential probes without a normative expectation."]}
    (args.output/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:report[k] for k in ["steps","by_scope","failures"]}),flush=True)
    return int(any(report["failures"].values()) or any(g.get("infrastructure_error") or g.get("cleanup_errors") for g in groups))


if __name__=="__main__":
    raise SystemExit(main())
