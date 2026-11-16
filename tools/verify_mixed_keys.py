"""Verify duplicate keys across legacy JSONL and binary storage."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.segments import Reader
from minivector.sql import execute


def main():
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


if __name__ == "__main__":
    main()
