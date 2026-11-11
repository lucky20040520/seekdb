"""Reproducible local vector workload and independent correctness checks.

The runner creates ``docs(id BIGINT PRIMARY KEY, tag VARCHAR(32),
v VECTOR(dimensions))`` before executing the generated SQL.  This
module generates and verifies data only; it does not start any process.
"""

import heapq
import json
import math
import random
import struct


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _vector_literal(vector):
    return "'" + json.dumps(vector, separators=(",", ":"), allow_nan=False) + "'"


def make_workload(rows=10000, dimensions=128, queries=1000, k=10,
                  data_seed=19017, query_seed=29017):
    """Generate records, 100-row inserts, and alternating query requests.

    ``requests`` contain the SQL plus its independent verification inputs.
    Five equally likely tags make each filtered query select roughly 20%.
    """
    for name, value in (("rows", rows), ("dimensions", dimensions),
                        ("queries", queries), ("k", k)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(name + " must be a positive integer")
    data_rng = random.Random(data_seed)
    records = [[i, "tag" + str(i % 5),
                [_f32(data_rng.uniform(-1.0, 1.0)) for _ in range(dimensions)]]
               for i in range(rows)]
    batches = []
    for start in range(0, rows, 100):
        values = ["(%d,'%s',%s)" % (rid, tag, _vector_literal(vector))
                  for rid, tag, vector in records[start:start + 100]]
        batches.append("INSERT INTO docs (id, tag, v) VALUES " + ",".join(values) + ";")
    query_rng = random.Random(query_seed)
    requests = []
    for i in range(queries):
        vector = [_f32(query_rng.uniform(-1.0, 1.0)) for _ in range(dimensions)]
        tag = "tag" + str(query_rng.randrange(5)) if i % 2 else None
        sql = "SELECT id, L2_DISTANCE(v, %s) AS distance FROM docs" % _vector_literal(vector)
        if tag is not None:
            sql += " WHERE tag = '%s'" % tag
        sql += " ORDER BY distance LIMIT %d;" % k
        requests.append({"sql": sql, "query": vector, "tag": tag})
    return {"records": records, "batches": batches, "requests": requests}


def _distance(left, right):
    if len(left) != len(right):
        raise ValueError("vector dimensions differ")
    return math.sqrt(math.fsum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def _rows(answer):
    if isinstance(answer, dict):
        if answer.get("ok") is False or "rows" not in answer:
            raise ValueError("query response is not successful or has no rows")
        return answer["rows"]
    return answer


def verify_answer(answer, records, query, tag, k=10):
    """Check a TopK response and compute recall against brute-force truth.

    A response can be the rows iterable or a successful JSON response object.
    Approximate IDs need not equal truth, but their distances and ordering must
    be correct.  A wrong count, duplicate ID, or filter violation is an error.
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError("k must be a positive integer")
    candidates = [record for record in records if tag is None or record[1] == tag]
    by_id = {record[0]: record for record in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("duplicate record ID")
    result = list(_rows(answer))
    expected_count = min(k, len(candidates))
    if len(result) != expected_count:
        raise ValueError("wrong result count")
    seen = set()
    prior = None
    for row in result:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("expected id and distance")
        rid, reported = row
        if isinstance(rid, bool) or not isinstance(rid, int) or rid not in by_id or rid in seen:
            raise ValueError("invalid, duplicate, or filtered-out ID")
        if isinstance(reported, bool) or not isinstance(reported, (int, float)) or not math.isfinite(reported):
            raise ValueError("invalid distance")
        distance = _distance(by_id[rid][2], query)
        if not math.isclose(reported, distance, rel_tol=1e-5, abs_tol=1e-5):
            raise ValueError("incorrect distance")
        key = (distance, rid)
        if prior is not None and key < prior:
            raise ValueError("results not ordered by distance and ID")
        prior = key
        seen.add(rid)
    truth = heapq.nsmallest(expected_count, candidates,
                            key=lambda row: (_distance(row[2], query), row[0]))
    return len(seen.intersection(row[0] for row in truth)) / expected_count if expected_count else 1.0


def verify_records(actual, records):
    """Require exactly the generated IDs, tags and complete float32 vectors."""
    expected = {row[0]: row for row in records}
    if len(expected) != len(records):
        raise ValueError("duplicate expected record ID")
    seen = set()
    for row in actual:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError("invalid record")
        rid, tag, vector = row
        if isinstance(rid, bool) or not isinstance(rid, int) or rid not in expected or rid in seen:
            raise ValueError("invalid or duplicate record ID")
        if tag != expected[rid][1] or not isinstance(vector, (list, tuple)):
            raise ValueError("record content differs")
        if len(vector) != len(expected[rid][2]):
            raise ValueError("record vector dimensions differ")
        for got, want in zip(vector, expected[rid][2]):
            if isinstance(got, bool) or not isinstance(got, (int, float)) or not math.isfinite(got) or got != want:
                raise ValueError("record vector differs")
        seen.add(rid)
    if len(seen) != len(expected):
        raise ValueError("missing records")
