"""Diagnose equality postings independently from the competition benchmark."""
import argparse
import inspect
import json
from pathlib import Path
import random
import re
import tempfile
import textwrap
import time

from minivector.engine import Database
from minivector.sql import execute
from minivector.vector import VectorCache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["repeated", "low-reuse", "over-budget"])
    parser.add_argument("postings", choices=["on", "off"])
    args = parser.parse_args()
    repeated = args.scenario == "repeated"
    oversized = args.scenario == "over-budget"
    # Change only the memoization decision in the baseline implementation.
    if args.postings == "off":
        source = inspect.getsource(VectorCache.candidate_indexes)
        source, count = re.subn(
            r"(?m)^    memoize = .*",
            "    memoize = False", textwrap.dedent(source), count=1)
        assert count == 1
        namespace = {}
        exec(source, namespace)
        VectorCache.candidate_indexes = namespace["candidate_indexes"]
    original = VectorCache.candidate_indexes
    filter_ns = cache_calls = 0

    def measure(self, conditions):
        nonlocal filter_ns, cache_calls
        started = time.perf_counter_ns()
        result = original(self, conditions)
        filter_ns += time.perf_counter_ns() - started
        cache_calls += 1
        return result

    VectorCache.candidate_indexes = measure
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        db = Database(directory)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,p VARCHAR(2048),v VECTOR(128));")
        rng = random.Random(19017)
        payload = "x" * 2048 if oversized else "x"
        keys = [rng.randrange(32) for _ in range(10000)]
        zero_tail = ",0" * 127
        for start in range(0, len(keys), 100):
            values = ",".join(
                f"({i},{i % 500},'{payload}','[{keys[i]}{zero_tail}]')"
                for i in range(start, start + 100))
            execute(db, "BEGIN;")
            execute(db, "INSERT INTO t VALUES " + values)
            execute(db, "COMMIT;")
        execute(db, "CREATE VECTOR INDEX idx ON t(v);")
        vector = "'[" + "0," * 127 + "0]'"
        tags = [7] * (12 if oversized else 500) if repeated or oversized else list(range(500))
        queries = [
            (f"SELECT id FROM t{where} ORDER BY L2_DISTANCE(v,{vector}) LIMIT 10;",
             sorted((i for i in range(len(keys)) if i % 500 == tag),
                    key=lambda i: (keys[i], i))[:10])
            for tag in tags for where in [f" WHERE tag={tag}"]
        ]
        list(execute(db, f"SELECT id FROM t ORDER BY L2_DISTANCE(v,{vector}) LIMIT 10;")[1])
        filter_ns = cache_calls = 0
        elapsed = 0.0
        for sql, expected in queries:
            started = time.perf_counter()
            rows = list(execute(db, sql)[1])
            elapsed += time.perf_counter() - started
            assert rows == [[key] for key in expected]
        print(json.dumps({"scenario": args.scenario, "postings": args.postings,
                          "queries": len(queries), "seconds": elapsed,
                          "filter_seconds": filter_ns / 1e9, "cache_calls": cache_calls,
                          "cache_bytes": db.query_cache_bytes}))
        db.close()


if __name__ == "__main__":
    main()
