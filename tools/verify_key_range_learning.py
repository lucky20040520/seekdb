"""Observe partial key scans and a deliberately invalid cache in isolated databases."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.sql import execute


def selected(db):
    return list(execute(db, "SELECT id FROM t ORDER BY id;")[1])


def trial(root, corrupt_cache):
    with closing(Database(root)) as db:
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
        db.insert("t", [[1], [100]])
        table = db.table("t")
        segment, = db.files(table)
        assert not db.key_ranges
        before = selected(db)
        files_before = list(db.files(table))
        interrupted_reads = []

        def interrupted(table, file):
            interrupted_reads.append(1)
            yield 1
            raise OSError("injected after first key")

        with patch.object(db, "file_primary_keys", interrupted):
            try:
                db.insert("t", [[50]])
            except OSError as error:
                error_text = str(error)
            else:
                raise AssertionError("injection did not fire")
        assert not db.key_ranges and not db.key_range_bytes
        assert selected(db) == before and db.files(db.table("t")) == files_before
        original = db.file_primary_keys
        reads = []

        def tracked(table, file):
            for key in original(table, file):
                reads.append(key)
                yield key

        if corrupt_cache:
            # Diagnostic counterfactual only. No production method is changed.
            db.key_ranges[segment] = (1, 1, 0)
        with patch.object(db, "file_primary_keys", tracked):
            try:
                db.insert("t", [[100]])
                response = "success"
            except DBError as error:
                response = error.code
        rows = selected(db)
        expected = [[1], [100], [100]] if corrupt_cache else [[1], [100]]
        assert rows == expected
        assert reads == ([] if corrupt_cache else [1, 100])
        assert response == ("success" if corrupt_cache else "DUPLICATE")
        return {"mode": "injected-invalid-cache" if corrupt_cache else "production",
                "interrupted_keys": interrupted_reads, "error": error_text,
                "cache_after_interruption": [], "rows_after_interruption": before,
                "retry_keys_read": reads, "retry_response": response, "rows": rows}


def overlap(root, key):
    with closing(Database(root)) as db:
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
        db.insert("t", [[1], [100]])
        table = db.table("t")
        segment, = db.files(table)
        # A complete successful check supplies the real cache entry, without
        # adding another data segment or inventing bounds in the diagnostic.
        db.check_primary_keys(table, {101})
        bounds = list(db.key_ranges[segment][:2])
        assert bounds == [1, 100]
        original = db.file_primary_keys
        reads = []
        calls = []

        def tracked(table, file):
            calls.append(file)
            for value in original(table, file):
                reads.append(value)
                yield value

        with patch.object(db, "file_primary_keys", tracked):
            try:
                db.insert("t", [[key]])
                response = "success"
            except DBError as error:
                response = error.code
        expected = [[1], [100]] if key == 100 else [[value] for value in sorted((1, 100, key))]
        rows = selected(db)
        assert rows == expected
        assert reads == ([] if key == 101 else [1, 100])
        assert len(calls) == (0 if key == 101 else 1)
        assert response == ("DUPLICATE" if key == 100 else "success")
    with closing(Database(root)) as reopened:
        assert selected(reopened) == expected
    return {"insert": key, "learned_bounds": bounds, "scan_calls": len(calls),
            "keys_read": reads, "response": response, "rows": rows,
            "reopened_rows": expected}


def migration(root):
    with closing(Database(root)) as db:
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
        db.insert("t", [[1], [100]])
        old_segment, = db.files(db.table("t"))
        db.check_primary_keys(db.table("t"), {101})
        before = dict(db.key_ranges)
        execute(db, "BEGIN;")
        try:
            execute(db, "UPDATE t SET id=150 WHERE id=100;")
        except DBError as error:
            update_error = {"code": error.code, "message": str(error)}
        else:
            raise AssertionError("primary key UPDATE unexpectedly accepted")
        assert selected(db) == [[1], [100]] and dict(db.key_ranges) == before
        assert execute(db, "DELETE FROM t WHERE id=100;") == 1
        assert execute(db, "INSERT INTO t VALUES (150);") == 1
        visible_files = list(db.files(db.table("t")))
        assert old_segment not in visible_files and selected(db) == [[1], [150]]
        db.check_primary_keys(db.table("t"), {151})
        names = {old_segment: "original"}
        for index, name in enumerate(visible_files):
            names[name] = "replacement-" + str(index)
        before_rollback = {names[name]: list(bounds[:2]) for name, bounds in db.key_ranges.items()}
        original = db.file_primary_keys
        reads = []

        def tracked(table, file):
            for key in original(table, file):
                reads.append([names[file], key])
                yield key

        with patch.object(db, "file_primary_keys", tracked):
            try:
                db.insert("t", [[150]])
            except DBError as error:
                transaction_response = error.code
            else:
                raise AssertionError("visible duplicate accepted")
        assert reads == [["replacement-1", 150]] and transaction_response == "DUPLICATE"
        inside_reads = list(reads)
        inside_rows = selected(db)
        execute(db, "ROLLBACK;")
        assert selected(db) == [[1], [100]]
        after_rollback = {names[name]: list(bounds[:2]) for name, bounds in db.key_ranges.items()}
        assert after_rollback == {"original": [1, 100]}
        reads.clear()
        with patch.object(db, "file_primary_keys", tracked):
            db.insert("t", [[150]])
        assert reads == [] and selected(db) == [[1], [100], [150]]
    with closing(Database(root)) as reopened:
        reopened_rows = selected(reopened)
        assert reopened_rows == [[1], [100], [150]]
    return {"update_error": update_error, "adjusted_operation": "DELETE 100 then INSERT 150 in one transaction",
            "visible_segments": [names[name] for name in visible_files],
            "ranges_before_rollback": before_rollback,
            "transaction_response": transaction_response, "transaction_reads": inside_reads,
            "transaction_rows": inside_rows, "ranges_after_rollback": after_rollback,
            "post_rollback_response": "success", "post_rollback_reads": reads,
            "reopened_rows": reopened_rows}


def restart(root):
    with closing(Database(root)) as db:
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
        db.insert("t", [[1], [100]])
        db.check_primary_keys(db.table("t"), {101})
        original_segment, = db.files(db.table("t"))
        execute(db, "BEGIN;")
        execute(db, "DELETE FROM t WHERE id=100;")
        execute(db, "INSERT INTO t VALUES (150);")
        db.check_primary_keys(db.table("t"), {151})
        execute(db, "ROLLBACK;")
        assert list(db.key_ranges[original_segment][:2]) == [1, 100]
        assert selected(db) == [[1], [100]]
    with closing(Database(root)) as db:
        assert not db.key_ranges
        original = db.file_primary_keys
        reads = []
        calls = []

        def tracked(table, file):
            calls.append(file)
            for key in original(table, file):
                reads.append(key)
                yield key

        with patch.object(db, "file_primary_keys", tracked):
            db.insert("t", [[150]])
        assert len(calls) == 1 and reads == [1, 100]
        rows = selected(db)
        assert rows == [[1], [100], [150]]
    with closing(Database(root)) as db:
        assert selected(db) == rows
    return {"cache_empty_on_reopen": True, "insert": 150, "scan_calls": len(calls),
            "keys_read": reads, "response": "success", "reopened_rows": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("initial", "overlap", "migration", "restart"), default="initial", nargs="?")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root:
        if args.stage == "restart":
            print(json.dumps(restart(root)))
        elif args.stage == "migration":
            print(json.dumps(migration(root)))
        elif args.stage == "overlap":
            for key in (100, 50, 101):
                print(json.dumps(overlap(Path(root) / str(key), key)))
        else:
            for mode in (False, True):
                print(json.dumps(trial(Path(root) / str(mode), mode)))
