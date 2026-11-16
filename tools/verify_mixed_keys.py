"""Verify duplicate keys across legacy JSONL and binary storage."""
import argparse
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.segments import Reader
from minivector.sql import execute


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=("initial", "migration"), default="initial")
    if parser.parse_args().scenario == "initial":
        initial()
    else:
        for outcome in ("ROLLBACK", "COMMIT"):
            migration(outcome)


if __name__ == "__main__":
    main()
