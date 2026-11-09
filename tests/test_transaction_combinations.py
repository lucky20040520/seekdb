"""Cross-table index creation plus failed statements at transaction boundaries."""
import tempfile
import unittest

from minivector.engine import Database, DBError
from minivector.sql import execute


class TransactionCombinations(unittest.TestCase):
    def test_create_index_and_failed_batch_across_tables_commit_or_rollback(self):
        for commit in (False, True):
            with self.subTest(commit=commit), tempfile.TemporaryDirectory() as directory:
                db = Database(directory)
                for table in ('a', 'b'):
                    execute(db, 'CREATE TABLE %s(id BIGINT PRIMARY KEY,s VARCHAR(2),v VECTOR(2));' % table)
                    execute(db, "INSERT INTO %s VALUES(1,'x','[1,0]'),(2,'y','[2,0]');" % table)
                execute(db, 'CREATE VECTOR INDEX ia ON a(v);')
                execute(db, 'BEGIN;')
                execute(db, 'CREATE VECTOR INDEX ib ON b(v);')
                execute(db, "UPDATE a SET v='[0,0]' WHERE id=2;")
                execute(db, 'DELETE FROM b WHERE id=1;')
                with self.assertRaises(DBError):
                    execute(db, "INSERT INTO b VALUES(3,'z','[0,0]'),(2,'z','[0,0]');")
                execute(db, "INSERT INTO b VALUES(4,'z','[0,0]');")
                for mode in ('exact', 'ann'):
                    self.assertEqual([[2],[1]], list(execute(db, "SELECT id FROM a ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;", mode)[1]))
                    self.assertEqual([[4],[2]], list(execute(db, "SELECT id FROM b ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;", mode)[1]))
                execute(db, 'COMMIT;' if commit else 'ROLLBACK;')
                db.close()
                for restart in range(2):
                    db = Database(directory)
                    self.assertEqual({'ia'}, set(db.table('a')['indexes']))
                    self.assertEqual({'ib'} if commit else set(), set(db.table('b')['indexes']))
                    for table in ('a', 'b'):
                        ordinary = list(execute(db, 'SELECT id FROM %s ORDER BY id;' % table)[1])
                        self.assertEqual([[2],[4]] if commit and table == 'b' else [[1],[2]], ordinary)
                        for mode in ('exact', 'ann'):
                            expected = ([[2],[1]] if table == 'a' else [[4],[2]]) if commit else [[1],[2]]
                            self.assertEqual(expected, list(execute(db, "SELECT id FROM %s ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;" % table, mode)[1]))
                    db.close()
