"""Observe cache admission with fixed records and varying scalar width."""
import json
import math
from pathlib import Path
import random
import tempfile
import time
from unittest.mock import patch

from minivector.engine import Database
from minivector.sql import execute


def trial(width, budget=16*1024*1024, filtered=True):
    with tempfile.TemporaryDirectory() as root:
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,p VARCHAR(2048),v VECTOR(128));")
        db.query_cache_limit = budget
        rng = random.Random(19017)
        rows = [[i, i % 10, "x" * width, [rng.uniform(-1, 1) for _ in range(128)]]
                for i in range(10000)]
        for start in range(0, len(rows), 100):
            db.insert("t", rows[start:start+100])
        truth = sorted(
            (math.sqrt(math.fsum(float(x) ** 2 for x in row[3])), row[0])
            for row in db.rows(db.table("t")) if not filtered or row[1] == 3
        )[:10]
        query = ("SELECT id,L2_DISTANCE(v," + str([0]*128) +
                 ") AS d FROM t " + ("WHERE tag=3 " if filtered else "") +
                 "ORDER BY d LIMIT 10;")
        cache_method = Database.cached_vectors
        row_method = Database.rows
        events, counts = [], {"rows_decoded": 0}

        def cached(database, table):
            result = cache_method(database, table)
            events.append({"available": result is not None,
                           "bytes": result.size if result is not None else 0})
            return result

        def scanned(database, table):
            for row in row_method(database, table):
                counts["rows_decoded"] += 1
                yield row

        with patch.object(Database, "cached_vectors", cached), patch.object(Database, "rows", scanned):
            started = time.perf_counter()
            first = list(execute(db, query)[1])
            first_seconds = time.perf_counter() - started
            first_decoded = counts["rows_decoded"]
            started = time.perf_counter()
            second = list(execute(db, query)[1])
            second_seconds = time.perf_counter() - started
        expected = [[key, distance] for distance, key in truth]
        assert first == second == expected
        db.close()
        print(json.dumps({"width": width, "rows": 10000, "dimension": 128,
                          "cache_budget": budget, "filtered": filtered, "cache_observations": events,
                          "first_rows_decoded": first_decoded,
                          "second_rows_decoded": counts["rows_decoded"]-first_decoded,
                          "first_seconds": first_seconds, "second_seconds": second_seconds,
                          "result": second, "verified": True}))


if __name__ == "__main__":
    import sys
    if "--controls" in sys.argv:
        for budget, filtered in ((16*1024*1024, True), (64*1024*1024, True),
                                 (16*1024*1024, False), (64*1024*1024, False)):
            trial(2048, budget, filtered)
    else:
        for width in (16, 2048):
            trial(width)
