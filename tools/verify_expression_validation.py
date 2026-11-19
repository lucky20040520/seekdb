"""Observe expression validation order and an isolated equality-cache counterfactual."""
from contextlib import closing, nullcontext
import argparse
import inspect
import json
import os
from pathlib import Path
import tempfile
import textwrap
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.segments import Reader
from minivector.sql import execute


def equality_variant():
    source = textwrap.dedent(inspect.getsource(Database.select))
    assert source.count("if previous is expr:") == 1
    source = source.replace("if previous is expr:", "if previous == expr:")
    namespace = dict(Database.select.__globals__)
    exec(compile(source, "isolated-value-equality-select", "exec"), namespace)
    return namespace["select"]


def observed(db, order, native, equality_cache):
    legal, illegal = [1, 0], [True, 0]
    arrays = (legal, illegal) if order == "valid-first" else (illegal, legal)
    if order == "boolean-only":
        arrays = (illegal,)
    calls, decoded = [], []
    validate, read_row = db.validate, Reader._row

    def tracked(column, value, for_storage=True):
        if column["kind"] == "VECTOR":
            calls.append({"input_type": type(value).__name__, "value": value})
        return validate(column, value, for_storage)

    def read(reader):
        decoded.append(1)
        return read_row(reader)

    gate = (patch.object(db, "select", equality_variant().__get__(db, Database))
            if equality_cache else nullcontext())
    with gate, patch.object(db, "validate", tracked), patch.object(Reader, "_row", read):
        try:
            if native:
                projections = [(('distance', 'v', vector), 'd' + str(i))
                               for i, vector in enumerate(arrays)]
                columns, rows = db.select('t', projections, order=(('column', 'd0'), False), limit=1)
            else:
                expressions = ["L2_DISTANCE(v,'" + json.dumps(vector, separators=(',', ':')).lower()
                               + "') AS d" + str(i) for i, vector in enumerate(arrays)]
                columns, rows = execute(db, 'SELECT ' + ','.join(expressions) + ' FROM t ORDER BY d0 LIMIT 1;')
            result = {"columns": columns, "rows": list(rows)}
        except DBError as error:
            result = {"error": error.code, "message": str(error)}
    expected_success = native and equality_cache and order == "valid-first"
    if expected_success:
        assert result["rows"] == [[0.0, 0.0]] and len(calls) == 1, result
    else:
        assert result["error"] == "TYPE" and not decoded, result
    return {"order": order, "input": "native-AST" if native else "SQL",
            "cache": "injected-value-equality" if equality_cache else "production-identity",
            "validation_calls": calls, "decoded_rows": len(decoded), "result": result}


def reuse(db):
    queries = [
        ("alias", "SELECT L2_DISTANCE(v,'[1,0]') AS d FROM t ORDER BY d LIMIT 1;", 1, [[0.0]]),
        ("separate", "SELECT L2_DISTANCE(v,'[1,0]') AS a,L2_DISTANCE(v,'[1,0]') AS b FROM t ORDER BY a LIMIT 1;", 2, [[0.0, 0.0]]),
        ("first-statement", "SELECT id,L2_DISTANCE(v,'[1,0]') AS d FROM t ORDER BY d LIMIT 1;", 1, [[1, 0.0]]),
        ("next-statement", "SELECT id,L2_DISTANCE(v,'[0,1]') AS d FROM t ORDER BY d LIMIT 1;", 1, [[2, 0.0]]),
        ("repeat-identical-statement", "SELECT id,L2_DISTANCE(v,'[0,1]') AS d FROM t ORDER BY d LIMIT 1;", 1, [[2, 0.0]]),
    ]
    results = []
    original = db.validate
    for label, sql, expected_calls, expected_rows in queries:
        calls = []

        def tracked(column, value, for_storage=True):
            if column['kind'] == 'VECTOR':
                calls.append(value)
            return original(column, value, for_storage)

        with patch.object(db, 'validate', tracked), \
                patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk:
            columns, rows = execute(db, sql)
            actual = list(rows)
        assert actual == expected_rows and len(calls) == expected_calls
        results.append({'case': label, 'validation_inputs': calls, 'rows': actual,
                        'disk_index_calls': disk.call_count})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('initial', 'reuse'), default='initial', nargs='?')
    args = parser.parse_args()
    if args.stage == 'reuse':
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'MINIVECTOR_NO_NUMPY': '0'}):
            for budget, indexed in ((16*1024*1024, False), (0, False), (0, True)):
                with closing(Database(Path(root) / (str(budget) + '-' + str(indexed)))) as db:
                    execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
                    db.insert('t', [[1, [1, 0]], [2, [0, 1]]])
                    db.query_cache_limit = budget
                    if indexed:
                        execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
                    results = reuse(db)
                    assert all(r['disk_index_calls'] == int(indexed) for r in results)
                    print(json.dumps({'budget': budget, 'indexed': indexed, 'queries': results}))
        raise SystemExit(0)
    with tempfile.TemporaryDirectory() as root:
        for native in (False, True):
            for equality_cache in (False, True):
                for order in ("valid-first", "boolean-first", "boolean-only"):
                    path = Path(root) / (str(native) + '-' + str(equality_cache) + '-' + order)
                    with closing(Database(path)) as db:
                        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
                        db.insert('t', [[1, [1, 0]], [2, [0, 1]]])
                        print(json.dumps(observed(db, order, native, equality_cache)), flush=True)
