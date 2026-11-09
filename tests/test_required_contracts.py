"""Requirement-derived cases: empty tables, comparisons, and bounded sorting."""
import tempfile
import tracemalloc
import unittest
import itertools
import math

from minivector.engine import Database, DBError
from minivector.sql import execute


class RequiredContracts(unittest.TestCase):
    def test_unicode_length_is_characters_at_storage_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(2));')
            execute(db,"INSERT INTO t VALUES (1,'汉😀');")
            with self.assertRaises(DBError):
                execute(db,"INSERT INTO t VALUES (2,'ab'),(3,'汉😀x');")
            self.assertEqual([[1,'汉😀']],list(execute(db,'SELECT * FROM t;')[1]))

    def test_primary_key_and_vector_need_not_be_first_or_last(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            definitions={'id':'id BIGINT PRIMARY KEY','tag':'tag VARCHAR(2)','v':'v VECTOR(2)'}
            values={'id':'1','tag':"'ab'",'v':'[3,4]'}
            for i,names in enumerate(itertools.permutations(definitions)):
                table='t%d'%i
                execute(db,'CREATE TABLE '+table+'('+','.join(definitions[n] for n in names)+');')
                execute(db,'INSERT INTO '+table+' VALUES ('+','.join(values[n] for n in names)+');')
                self.assertEqual([[1,'ab',5.0]],list(execute(db,
                    'SELECT id,tag,L2_DISTANCE(v,[0,0]) AS d FROM '+table+" WHERE tag='ab' ORDER BY d LIMIT 10;")[1]))

    def test_extreme_finite_float32_distance_and_zero_ties(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            maximum=3.4028234663852886e38
            db.insert('t',[[2,[maximum,maximum]],[1,[-maximum,-maximum]]])
            result=list(execute(db,'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 2;')[1])
            self.assertEqual([1,2],[row[0] for row in result])
            self.assertTrue(all(math.isfinite(row[1]) for row in result))
            execute(db,'UPDATE t SET v=[-0.0,0.0];')
            self.assertEqual([[1],[2]],list(execute(db,'SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2;')[1]))

    def test_empty_tables_keep_distinct_filter_schemas(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db, 'CREATE TABLE a(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(2));')
            execute(db, 'CREATE TABLE b(id BIGINT PRIMARY KEY,category VARCHAR(3),v VECTOR(3));')
            self.assertEqual([], list(execute(db, 'SELECT id FROM a WHERE tag=1 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;')[1]))
            self.assertEqual([], list(execute(db, "SELECT id FROM b WHERE category='x' ORDER BY L2_DISTANCE(v,[0,0,0]) LIMIT 10;")[1]))

    def test_comparison_literal_is_not_a_stored_varchar(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(2));')
            execute(db, "INSERT INTO t VALUES (1,'ab');")
            self.assertEqual([], list(execute(db, "SELECT id FROM t WHERE tag='abc';")[1]))
            self.assertEqual([[1]], list(execute(db, "SELECT id FROM t WHERE tag<'abc';")[1]))
            self.assertEqual([], list(execute(db, "SELECT id FROM t WHERE tag>'abc';")[1]))

    def test_wide_records_do_not_accumulate_behind_small_numeric_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(65536));')
            db.rewrite('t', ([i, 'x' * 65536] for i in range(300)))
            tracemalloc.start()
            try:
                rows = list(execute(db, 'SELECT id FROM t ORDER BY payload LIMIT 1000;')[1])
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual([[i] for i in range(300)], rows)
            self.assertLess(peak, 12 * 1024 * 1024,
                            'LIMIT must not make the executor retain all wide records')


if __name__ == '__main__':
    unittest.main()
