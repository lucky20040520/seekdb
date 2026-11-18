"""Count decoded binary rows and vector scan work around LIMIT 0 on a small table."""
import argparse
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.segments import Reader
from minivector.sql import execute
from minivector.vector import VectorCache


TOPK = "SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT {};"
VALID = TOPK.format(0)
UNKNOWN = "SELECT id FROM t ORDER BY L2_DISTANCE(missing,[0,0]) LIMIT 0;"
WRONG_DIM = "SELECT id FROM t ORDER BY L2_DISTANCE(v,[0]) LIMIT 0;"


def observed(db, sql):
    counter = {"decoded_rows": 0, "squared_scan_rows": 0}
    decode, squared = Reader._row, VectorCache.squared_distances

    def read_row(reader):
        row = decode(reader)
        counter["decoded_rows"] += 1
        return row

    def scan(cache, indexes, query, size, dtype):
        counter["squared_scan_rows"] += size
        return squared(cache, indexes, query, size, dtype)

    with patch.object(Reader, "_row", read_row), patch.object(VectorCache, "squared_distances", scan), \
            patch.object(VectorCache, "build", wraps=VectorCache.build) as build:
        try:
            headers, rows = execute(db, sql)
            result = {"columns": headers, "rows": list(rows)}
        except DBError as error:
            result = {"error": error.code}
        return dict(result, **counter, cache_builds=build.call_count, cache_entries=len(db.query_cache))


def prepare(db):
    execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));")
    values = ",".join(f"({i},[{i},0])" for i in range(64))
    assert execute(db, f"INSERT INTO t VALUES {values};") == 64
    assert not db.query_cache


def basic(db):
    result = {"unknown": observed(db, UNKNOWN), "cold": observed(db, VALID),
              "warm": observed(db, VALID)}
    assert result["unknown"]["error"] == "SCHEMA", result
    assert result["cold"]["rows"] == result["warm"]["rows"] == [], result
    return result


def sequence(db, skip_zero_cache, positive_limit):
    # Only bypass cache access during the zero-limit statements. SQL still binds
    # every expression. The positive-limit query always uses the normal executor.
    gate = patch.object(db, "cached_vectors", return_value=None) if skip_zero_cache else nullcontext()
    with gate:
        unknown, wrong_dim = observed(db, UNKNOWN), observed(db, WRONG_DIM)
        zeros = [observed(db, VALID), observed(db, VALID)]
    positive = observed(db, TOPK.format(positive_limit))
    assert (unknown["error"], wrong_dim["error"]) == ("SCHEMA", "TYPE")
    assert all(q["columns"] == ["id"] and q["rows"] == [] for q in zeros), zeros
    assert positive["columns"] == ["id"] and positive["rows"] == [[i] for i in range(positive_limit)], positive
    results = zeros + [positive]
    return {"plan": "bypass-on-zero" if skip_zero_cache else "baseline",
            "errors": [unknown["error"], wrong_dim["error"]],
            "decoded": [q["decoded_rows"] for q in results],
            "squared_scan": [q["squared_scan_rows"] for q in results],
            "builds": [q["cache_builds"] for q in results],
            "cache_entries": [q["cache_entries"] for q in results],
            "rows": [q["rows"] for q in results]}


def changed_snapshot(db, skip_zero_cache):
    previous = tuple(db.files(db.table("t")))
    gate = patch.object(db, "cached_vectors", return_value=None) if skip_zero_cache else nullcontext()
    with gate:
        first = observed(db, VALID)
    execute(db, "BEGIN;")
    assert execute(db, "INSERT INTO t VALUES (64,[0,0]);") == 1
    execute(db, "COMMIT;")
    current = tuple(db.files(db.table("t")))
    assert previous != current
    old_cached = previous in db.query_cache
    new_cached = current in db.query_cache
    final = observed(db, TOPK.format(10))
    expected_ids = [0, 64] + list(range(1, 9))
    assert first["columns"] == final["columns"] == ["id"] and first["rows"] == []
    assert final["rows"] == [[key] for key in expected_ids], final
    return {"plan": "bypass-on-zero" if skip_zero_cache else "baseline",
            "signature_changed": previous != current,
            "old_cached_after_commit": old_cached, "new_cached_before_topk": new_cached,
            "new_cached_after_topk": current in db.query_cache,
            "decoded": [first["decoded_rows"], final["decoded_rows"]],
            "builds": [first["cache_builds"], final["cache_builds"]],
            "squared_scan": [first["squared_scan_rows"], final["squared_scan_rows"]],
            "top_ids": [row[0] for row in final["rows"]]}


if __name__ == "__main__":
    import numpy  # Exercise the optional cache path in this probe.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("basic", "sequence", "budget", "k10", "snapshot"), default="basic", nargs="?")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"MINIVECTOR_NO_NUMPY": "0"}):
        if args.stage in ("sequence", "k10", "snapshot"):
            limit = 10 if args.stage == "k10" else 1
            if args.stage == "k10":
                with closing(Database(Path(root) / "direct")) as db:
                    prepare(db)
                    direct = observed(db, TOPK.format(10))
                    assert direct["columns"] == ["id"] and direct["rows"] == [[i] for i in range(10)], direct
                    print(json.dumps({"plan": "cold-k10-only", **direct}))
            for skip in (False, True):
                with closing(Database(Path(root) / str(skip))) as db:
                    prepare(db)
                    result = changed_snapshot(db, skip) if args.stage == "snapshot" else sequence(db, skip, limit)
                    print(json.dumps(result))
        else:
            with closing(Database(root)) as db:
                prepare(db)
                if args.stage == "budget":
                    db.query_cache_limit = 1
                print(json.dumps(basic(db)))
