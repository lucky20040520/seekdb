"""Storage transition, corruption bounds, and cache-admission regressions."""
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minivector import segments
from minivector.engine import Database
from minivector.sql import execute


class SegmentContracts(unittest.TestCase):
    def test_legacy_without_sidecar_mixed_index_rollback_and_rewrite(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(v VECTOR(2),s VARCHAR(8),id BIGINT PRIMARY KEY);')
            old = Path(root) / 'rows-legacy.jsonl'
            old.write_text(json.dumps([[3.0,4.0], '\u6c49\U0001f600', -2]) + '\n', encoding='utf-8')
            catalog = copy.deepcopy(db.catalog)
            catalog['t']['file'] = old.name
            db.publish(catalog)
            self.assertEqual([-2], list(db.primary_keys(db.table('t'))))
            execute(db, "INSERT INTO t VALUES('[0,0]','new',7);")
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            db.query_cache_limit = 1
            sql = "SELECT id,s,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE id>=-2 ORDER BY d LIMIT 2;"
            truth = [[7,'new',0.0],[-2,'\u6c49\U0001f600',5.0]]
            self.assertEqual(truth, list(execute(db, sql, 'exact')[1]))
            execute(db, 'BEGIN;')
            execute(db, 'DELETE FROM t WHERE id=-2;')
            execute(db, 'ROLLBACK;')
            self.assertTrue(old.exists())
            self.assertEqual(truth, list(execute(db, sql)[1]))
            execute(db, "UPDATE t SET s='kept' WHERE id=7;")
            self.assertFalse(old.exists())
            self.assertTrue(all(p.endswith('.mvb') for p in db.files(db.table('t'))))
            self.assertEqual([], list(Path(root).glob('*.keys')))
            db.close()
            db = Database(root)
            truth[0][1] = 'kept'
            self.assertEqual(truth, list(execute(db, sql)[1]))
            self.assertEqual([-2,7], list(db.primary_keys(db.table('t'))))

    def test_binary_reader_rejects_truncation_and_invalid_lengths(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(2),v VECTOR(2));')
            execute(db, "INSERT INTO t VALUES(1,'ab','[1,2]');")
            file = Path(root) / db.files(db.table('t'))[0]
            original = file.read_bytes()
            mutations = [original[:-1], original[:20]]
            for offset, value in ((24, 2**63), (32, 2**63), (48, 2**63)):
                value_bytes = bytearray(original)
                struct.pack_into('<Q', value_bytes, offset, value)
                mutations.append(value_bytes)
            for value in mutations:
                file.write_bytes(value)
                with self.assertRaises(OSError):
                    with segments.Reader(file, db.table('t')['columns']) as source:
                        list(source)

    def test_wide_binary_rows_reject_cache_before_decoding(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(1000),v VECTOR(2));')
            db.insert('t', [[i, 'x'*1000, [1.0,2.0]] for i in range(100)])
            db.query_cache_limit = 50 * 1024
            with patch.object(db, 'rows', side_effect=AssertionError('wide rows decoded for cache')):
                self.assertIsNone(db.cached_vectors(db.table('t')))
            self.assertEqual({}, db.query_cache)


if __name__ == '__main__':
    unittest.main()
