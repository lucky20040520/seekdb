import tempfile
import unittest
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.sql import execute


class KeyRangeContracts(unittest.TestCase):
    def test_disjoint_segments_skip_reads_but_overlapping_keys_are_checked(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            db.insert('t', [[-10], [10]])
            db.insert('t', [[20]])  # Learns [-10, 10] from a complete scan.
            first, second = db.files(db.table('t'))
            original = db.file_primary_keys
            reads = []
            def tracked(table, file):
                reads.append(file)
                yield from original(table, file)
            with patch.object(db, 'file_primary_keys', tracked):
                db.insert('t', [[30]])
                self.assertEqual([second], reads)
                reads.clear()
                db.insert('t', [[0]])  # Overlapping range without a duplicate.
                self.assertIn(first, reads)
                with self.assertRaises(DBError) as error:
                    db.insert('t', [[100], [-10]])
                self.assertEqual('DUPLICATE', error.exception.code)
            self.assertEqual([[-10], [0], [10], [20], [30]],
                             list(execute(db, 'SELECT id FROM t ORDER BY id;')[1]))
            db.close()
            db = Database(root)
            self.assertFalse(db.key_ranges)
            with self.assertRaises(DBError):
                db.insert('t', [[20]])
            db.close()

    def test_rollback_and_rewrite_do_not_retain_dead_ranges(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            db.insert('t', [[1]])
            db.insert('t', [[2]])
            db.begin()
            db.insert('t', [[100]])
            db.insert('t', [[101]])
            rolled_back = db.files(db.table('t'))[-2]
            self.assertIn(rolled_back, db.key_ranges)
            db.rollback()
            self.assertNotIn(rolled_back, db.key_ranges)
            db.insert('t', [[100]])
            execute(db, 'DELETE FROM t WHERE id=1;')
            self.assertFalse(db.key_ranges)
            self.assertEqual(0, db.key_range_bytes)
            db.insert('t', [[1]])
            execute(db, 'DELETE FROM t;')
            self.assertFalse(db.key_ranges)
            self.assertEqual(0, db.key_range_bytes)
            db.close()

    def test_saturated_disabled_cache_empty_segments_and_bigint_edges(self):
        for budget in (0, 1400):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as root:
                db = Database(root)
                db.key_range_limit = budget
                execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
                db.insert('t', [])
                for key in [-(2**63), *range(8), 2**63-1]:
                    db.insert('t', [[key]])
                    self.assertLessEqual(db.key_range_bytes, budget)
                if budget:
                    self.assertLess(len(db.key_ranges), len(db.files(db.table('t'))))
                    self.assertLessEqual(db.key_range_bytes + 256, budget)
                    admitted = set(db.key_ranges)
                    db.insert('t', [[100]])
                    self.assertEqual(admitted, set(db.key_ranges))
                else:
                    self.assertFalse(db.key_ranges)
                for key in [-(2**63), 2**63-1, 0]:
                    with self.assertRaises(DBError):
                        db.insert('t', [[key]])
                db.close()

    def test_incomplete_scan_never_supplies_a_range(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            db.insert('t', [[1], [100]])
            def interrupted(table, file):
                yield 1
                raise OSError('interrupted footer read')
            with patch.object(db, 'file_primary_keys', interrupted):
                with self.assertRaises(OSError):
                    db.insert('t', [[50]])
            self.assertFalse(db.key_ranges)
            with self.assertRaises(DBError):
                db.insert('t', [[100]])
            db.close()


if __name__ == '__main__':
    unittest.main()
