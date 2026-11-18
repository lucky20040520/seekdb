"""Exercise failed vector batches, rollback and both exact execution paths."""
import json
from pathlib import Path
import tempfile
from tools.verify_commit_cut import Session

QUERY = "SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 10;"


def trial(outcome, disk):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with Session(root, numpy_enabled=not disk) as session:
            session.request(1, "CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));")
            session.request(2, "INSERT INTO t VALUES (1,[1,0]);")
            if disk:
                session.request(3, "CREATE VECTOR INDEX idx ON t(v);")
            session.request(4, "BEGIN;")
            session.request(5, "UPDATE t SET v=[0,0] WHERE id=1;")
            warmed = session.request(6, QUERY)["rows"]
            session.send(7, "INSERT INTO t VALUES (2,[2,0]),(3,[3]);")
            error = session.receive()
            assert error["ok"] is False and error["error"]["code"] == "TYPE", error
            rows = session.request(8, "SELECT * FROM t ORDER BY id;")["rows"]
            assert rows == [[1, [0.0, 0.0]]]
            session.request(9, "INSERT INTO t VALUES (4,[4,0]);")
            before = session.request(10, QUERY)["rows"]
            assert before == [[1, 0.0], [4, 4.0]]
            session.request(11, outcome + ";")
            immediate = session.request(12, QUERY)["rows"]
            expected = [[1, 1.0]] if outcome == "ROLLBACK" else before
            assert immediate == expected
        with Session(root, numpy_enabled=not disk) as reopened:
            after = reopened.request(1, QUERY)["rows"]
            assert after == expected
        print(json.dumps({"outcome": outcome, "path": "disk_index" if disk else "default_cache",
                          "warmed_transaction": warmed, "error": error,
                          "select_after_failure": rows, "topk_before_end": before,
                          "immediate_after_end": immediate, "after_restart": after,
                          "passed": True}))


def observed_cache():
    from unittest.mock import patch
    from minivector.engine import Database
    from minivector.sql import execute
    from minivector.vector import VectorCache

    events = []
    cache_method = Database.cached_vectors
    topk_method = VectorCache.topk

    def cached(db, table):
        signature = tuple(db.files(table))
        hit = signature in db.query_cache
        value = cache_method(db, table)
        events.append({"kind": "lookup", "hit": hit, "available": value is not None})
        return value

    def topk(cache, *args, **kwargs):
        events.append({"kind": "topk"})
        return topk_method(cache, *args, **kwargs)

    with tempfile.TemporaryDirectory() as root:
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));")
        execute(db, "INSERT INTO t VALUES (1,[1,0]);")
        with patch.object(Database, "cached_vectors", cached), patch.object(VectorCache, "topk", topk):
            execute(db, "BEGIN;")
            execute(db, "UPDATE t SET v=[0,0] WHERE id=1;")
            for _ in range(3):
                assert list(execute(db, QUERY)[1]) == [[1, 0.0]]
            before = list(events)
            assert sum(e["kind"] == "topk" for e in before) == 3
            assert sum(e.get("hit", False) for e in before) == 2
            execute(db, "ROLLBACK;")
            entries_after_rollback = len(db.query_cache)
            result = list(execute(db, QUERY)[1])
            assert result == [[1, 1.0]]
            assert events[-1]["kind"] == "topk"
            assert events[-2] == {"kind": "lookup", "hit": False, "available": True}
        db.close()
    print(json.dumps({"test": "observed_cache", "before_rollback": before,
                      "cache_entries_after_rollback": entries_after_rollback,
                      "after_rollback_events": events[len(before):],
                      "immediate_result": result, "passed": True}))


if __name__ == "__main__":
    import sys
    if "--observe-cache" in sys.argv:
        observed_cache()
        raise SystemExit(0)
    for disk in (False, True):
        for outcome in ("COMMIT", "ROLLBACK"):
            trial(outcome, disk)
