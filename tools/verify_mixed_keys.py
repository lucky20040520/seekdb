"""Verify duplicate keys across legacy JSONL and binary storage."""
import argparse
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.segments import Reader
from minivector.sql import execute
from tools.verify_commit_cut import Session


def initial():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
        legacy = root / "rows-legacy.jsonl"
        legacy.write_text("[1]\n", encoding="utf-8")
        db.catalog["t"]["file"] = legacy.name
        db.publish(db.catalog)
        assert not Path(str(legacy) + ".keys").exists()
        execute(db, "INSERT INTO t VALUES (2);")
        db.close()
        db = Database(root)
        original_keys = Reader.keys
        trace = []

        def trace_keys(reader, index):
            for key in original_keys(reader, index):
                trace.append({"key": key, "path": "binary_footer" if reader.binary else "legacy_jsonl"})
                yield key

        for values, expected_trace, expected_ids in [
            ("(1)", [1], [1, 2]),
            ("(2)", [1, 2], [1, 2]),
            ("(3),(3)", [], [1, 2]),
            ("(1),(1)", [], [1, 2]),
            ("(1),(3)", [1], [1, 2]),
            ("(3)", [1, 2], [1, 2, 3]),
        ]:
            trace.clear()
            before = (root / "catalog.json").read_bytes()
            files = sorted(root.iterdir())
            with patch.object(Reader, "keys", trace_keys):
                try:
                    affected = execute(db, "INSERT INTO t VALUES " + values + ";")
                    response = {"ok": True, "affected_rows": affected}
                except DBError as error:
                    response = {"ok": False, "error": {"code": error.code, "message": str(error)}}
            ids = [row[0] for row in execute(db, "SELECT id FROM t ORDER BY id;")[1]]
            unchanged = before == (root / "catalog.json").read_bytes() and files == sorted(root.iterdir())
            assert ids == expected_ids
            assert [item["key"] for item in trace] == expected_trace
            assert all(item["path"] == ("legacy_jsonl" if item["key"] == 1 else "binary_footer")
                       for item in trace)
            if values == "(3)":
                assert response == {"ok": True, "affected_rows": 1}
            else:
                assert not response["ok"] and response["error"]["code"] and unchanged
            print(json.dumps({"values": values, "response": response, "ids": ids,
                              "catalog_and_file_list_unchanged": unchanged, "key_reads": trace}))
        db.close()


def migration(outcome):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY, label VARCHAR(16));")
        legacy = root / "rows-legacy.jsonl"
        legacy.write_text('[1,"old"]\n', encoding="utf-8")
        db.catalog["t"]["file"] = legacy.name
        db.publish(db.catalog)
        execute(db, "INSERT INTO t VALUES (2,'new');")
        assert legacy.name in db.files(db.table("t"))
        assert not Path(str(legacy) + ".keys").exists()
        execute(db, "BEGIN;")
        execute(db, "DELETE FROM t WHERE id=1;")
        catalog = db.catalog_path.read_bytes()
        trace = []
        original = Reader.keys

        def tracked(reader, index):
            for key in original(reader, index):
                trace.append(key)
                yield key

        with patch.object(Reader, "keys", tracked):
            affected = execute(db, "INSERT INTO t VALUES (1,'replacement');")
        sql = "SELECT * FROM t ORDER BY id;"
        in_transaction = list(execute(db, sql)[1])
        assert db.catalog_path.read_bytes() == catalog
        execute(db, outcome + ";")
        db.close()
        reopened = Database(root)
        after = list(execute(reopened, sql)[1])
        reopened.close()
        assert affected == 1 and trace == [2]
        assert in_transaction == [[1, "replacement"], [2, "new"]]
        assert after == [[1, "old" if outcome == "ROLLBACK" else "replacement"], [2, "new"]]
        print(json.dumps({"outcome": outcome, "key_reads_on_reinsert": trace,
                          "in_transaction": in_transaction, "after_reopen": after,
                          "legacy_file_survives": legacy.exists(), "passed": True}))


def crash_before_cleanup():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY, label VARCHAR(16));")
        legacy = root / "rows-legacy.jsonl"
        legacy.write_text('[1,"old"]\n', encoding="utf-8")
        db.catalog["t"]["file"] = legacy.name
        db.publish(db.catalog)
        execute(db, "INSERT INTO t VALUES (2,'new');")
        db.close()
        with Session(root, "after") as writer:
            writer.request(1, "BEGIN;")
            writer.request(2, "DELETE FROM t WHERE id=1;")
            writer.request(3, "INSERT INTO t VALUES (1,'replacement');")
            writer.send(4, "COMMIT;")
            assert writer.stderr.get(timeout=10) == "CUT after"
            assert writer.stdout.empty()
        assert legacy.exists()
        table = json.loads((root / "catalog.json").read_bytes())["t"]
        assert legacy.name not in Database.files(table)
        reopened = Database(root)
        rows = list(execute(reopened, "SELECT * FROM t ORDER BY id;")[1])
        assert rows == [[1, "replacement"], [2, "new"]]
        assert not legacy.exists()
        try:
            execute(reopened, "INSERT INTO t VALUES (1,'duplicate');")
        except DBError as error:
            assert error.code == "DUPLICATE"
        else:
            raise AssertionError("duplicate live replacement accepted")
        execute(reopened, "BEGIN;")
        execute(reopened, "DELETE FROM t WHERE id=1;")
        assert execute(reopened, "INSERT INTO t VALUES (1,'again');") == 1
        execute(reopened, "ROLLBACK;")
        reopened.close()
        print(json.dumps({"commit_response": None, "legacy_exists_before_recovery": True,
                          "legacy_referenced_by_published_catalog": False,
                          "legacy_exists_after_recovery": False, "restarted_rows": rows,
                          "live_duplicate_rejected": True, "reinsert_after_delete": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=("initial", "migration", "crash"), default="initial")
    scenario = parser.parse_args().scenario
    if scenario == "initial":
        initial()
    elif scenario == "crash":
        crash_before_cleanup()
    else:
        for outcome in ("ROLLBACK", "COMMIT"):
            migration(outcome)


if __name__ == "__main__":
    main()
