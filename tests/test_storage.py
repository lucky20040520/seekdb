import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from minivectordb.storage import Database, DatabaseError


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name)
        self.db.create_table("docs", {"id": ["BIGINT", None], "text": ["VARCHAR", 4], "v": ["VECTOR", 2]}, "id")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_atomic_insert_and_validation(self):
        self.db.insert("docs", [{"id": 1, "text": "中文", "v": [0.1, 2]}])
        with self.assertRaises(DatabaseError):
            self.db.insert("docs", [{"id": 2, "text": "x", "v": [1, 2]}, {"id": 1, "text": "y", "v": [1, 2]}])
        with self.assertRaises(DatabaseError):
            self.db.insert("docs", [{"id": 3, "text": "x", "v": [float("inf"), 2]}])
        self.assertEqual([r["id"] for r in self.db.rows("docs")], [1])

    def test_transaction_visibility_search_and_rollback(self):
        self.db.insert("docs", [{"id": 2, "text": "x", "v": [1, 0]}, {"id": 1, "text": "x", "v": [-1, 0]}, {"id": 3, "text": "y", "v": [0, 0]}])
        self.db.begin()
        self.db.delete("docs", lambda r: r["id"] == 1)
        self.db.update("docs", lambda r: r["id"] == 2, {"v": [0, 0]})
        detached = self.db.get("docs", 2)
        detached["v"][0] = 100
        self.assertEqual(self.db.get("docs", 2)["v"], [0.0, 0.0])
        self.assertEqual([x[1] for x in self.db.search("docs", "v", [0, 0], 2, lambda r: r["text"] == "x")], [2])
        self.db.rollback()
        self.assertEqual([x[1] for x in self.db.search("docs", "v", [0, 0], 2, lambda r: r["text"] == "x")], [1, 2])

    def test_crash_and_torn_record_recovery(self):
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        self.db.close()
        code = "from minivectordb.storage import Database; import os,sys; d=Database(sys.argv[1]); d.begin(); d.insert('docs',[{'id':2,'text':'y','v':[1,2]}]); os._exit(0)"
        subprocess.run([sys.executable, "-c", code, self.tmp.name], check=True)
        with open(os.path.join(self.tmp.name, "data.wal"), "ab") as f:
            f.write(b'[["put",')
        self.db = Database(self.tmp.name)
        self.assertEqual([r["id"] for r in self.db.rows("docs")], [1])
        self.db.insert("docs", [{"id": 3, "text": "z", "v": [1, 2]}])
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual([r["id"] for r in self.db.rows("docs")], [1, 3])

    def test_uncommitted_complete_records_are_discarded_on_repeated_recovery(self):
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        self.db.close()
        with open(os.path.join(self.tmp.name, "data.wal"), "ab") as f:
            f.write(b'["begin"]\n["put","docs",2,{"id":2,"text":"y","v":[1,2]}]\n')
        for _ in range(2):
            self.db = Database(self.tmp.name)
            self.assertEqual([r["id"] for r in self.db.rows("docs")], [1])
            self.db.close()
        self.db = Database(self.tmp.name)
        self.db.insert("docs", [{"id": 3, "text": "z", "v": [1, 2]}])
        self.assertEqual([r["id"] for r in self.db.rows("docs")], [1, 3])

    def test_commit_io_failure_requires_reopen(self):
        self.db.begin()
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        with patch("minivectordb.storage.os.fsync", side_effect=OSError("injected")):
            with self.assertRaises(DatabaseError):
                self.db.commit()
        with self.assertRaises(DatabaseError):
            self.db.get("docs", 1)
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertIn(self.db.get("docs", 1), [None, {"id": 1, "text": "x", "v": [1.0, 2.0]}])

    def test_disk_index_rebuilt_after_committed_updates(self):
        self.db.insert("docs", [{"id": i, "text": "x", "v": [1, 2]} for i in range(150)])
        self.db.delete("docs", lambda row: row["id"] % 3 == 0)
        self.db.update("docs", lambda row: row["id"] == 149, {"text": "new"})
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual([row["id"] for row in self.db.rows("docs")], [i for i in range(150) if i % 3])
        self.assertEqual(self.db.get("docs", 149)["text"], "new")

    def test_process_exit_during_recovery_can_be_retried(self):
        self.db.insert("docs", [{"id": i, "text": "x", "v": [1, 2]} for i in range(20)])
        self.db.close()
        code = ("import os,sys; from minivectordb.page_index import PageIndex; "
                "PageIndex.put=lambda *args: os._exit(17); "
                "from minivectordb.storage import Database; Database(sys.argv[1])")
        result = subprocess.run([sys.executable, "-c", code, self.tmp.name])
        self.assertEqual(result.returncode, 17)
        self.db = Database(self.tmp.name)
        self.assertEqual([r["id"] for r in self.db.rows("docs")], list(range(20)))

    def test_failed_streaming_statement_preserves_prior_transaction(self):
        self.db.begin()
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        def rows():
            for i in range(2, 200):
                yield {"id": i, "text": "x", "v": [1, 2]}
            raise DatabaseError("injected validation failure")
        with self.assertRaises(DatabaseError):
            self.db.insert("docs", rows())
        self.assertEqual([r["id"] for r in self.db.rows("docs")], [1])
        self.db.commit()
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual([r["id"] for r in self.db.rows("docs")], [1])

    def test_checkpoint_reclaims_wal_and_recovers_new_commits(self):
        self.db.insert("docs", [{"id": i, "text": "x", "v": [1, 2]} for i in range(40)])
        old_wal = self.db.path
        self.db.checkpoint()
        self.assertFalse(old_wal.exists())
        self.assertEqual(self.db.path.stat().st_size, 0)
        self.db.insert("docs", [{"id": 50, "text": "new", "v": [3, 4]}])
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual([r["id"] for r in self.db.rows("docs")], list(range(40)) + [50])
        previous_snapshot = self.db.checkpoint_path
        self.db.checkpoint()
        self.assertFalse(previous_snapshot.exists())
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual(self.db.get("docs", 50)["text"], "new")

    def test_threshold_triggers_checkpoint(self):
        self.db.checkpoint_bytes = 1
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        self.assertIsNotNone(self.db.checkpoint_pages)
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual(self.db.get("docs", 1)["text"], "x")

    def test_checkpoint_survives_process_exit_before_and_after_publish(self):
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        self.db.close()
        before = ("import os,sys; from minivectordb.storage import Database; "
                  "d=Database(sys.argv[1]); os.replace=lambda *a: os._exit(21); d.checkpoint()")
        self.assertEqual(subprocess.run([sys.executable, "-c", before, self.tmp.name]).returncode, 21)
        self.db = Database(self.tmp.name)
        self.assertEqual(self.db.get("docs", 1)["text"], "x")
        self.db.close()
        after = ("import os,sys; from minivectordb.storage import Database; "
                 "d=Database(sys.argv[1]); orig=os.replace; "
                 "def_die='def die(*a):\\n orig(*a)\\n os._exit(22)'; "
                 "exec(def_die); os.replace=die; d.checkpoint()")
        self.assertEqual(subprocess.run([sys.executable, "-c", after, self.tmp.name]).returncode, 22)
        self.db = Database(self.tmp.name)
        self.assertEqual(self.db.get("docs", 1)["text"], "x")

    def test_checkpoint_does_not_overwrite_unrelated_existing_data_file(self):
        path = os.path.join(self.tmp.name, "rows.data")
        with open(path, "wb") as f:
            f.write(b"existing unrelated data")
        self.db.checkpoint()
        self.db.close()
        self.db = Database(self.tmp.name)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"existing unrelated data")

    def test_checkpoint_failures_preserve_recoverable_state(self):
        self.db.insert("docs", [{"id": 1, "text": "x", "v": [1, 2]}])
        old_wal = self.db.path
        with patch("minivectordb.storage.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(DatabaseError):
                self.db.checkpoint()
        self.assertTrue(old_wal.exists())
        self.assertIsNotNone(self.db.get("docs", 1))
        with patch.object(self.db, "_sync_directory", side_effect=[None, OSError("injected")]):
            with self.assertRaises(DatabaseError):
                self.db.checkpoint()
        self.assertTrue(old_wal.exists())
        self.db.close()
        self.db = Database(self.tmp.name)
        self.assertEqual(self.db.get("docs", 1)["text"], "x")
        self.db.begin()
        with self.assertRaises(DatabaseError):
            self.db.checkpoint()
        self.db.rollback()


if __name__ == "__main__":
    unittest.main()
