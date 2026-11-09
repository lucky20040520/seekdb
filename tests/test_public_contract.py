import tempfile
import unittest

from minivector.engine import Database, DBError
from minivector.sql import execute
from tools.run_public import check


class PublishedVectorSyntax(unittest.TestCase):
    def test_quoted_vector_is_type_directed_and_float32(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(directory)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(20),v VECTOR(2));')
            execute(db,"INSERT INTO t VALUES (2,'[3,4]','[16777217,0]'),(1,'[0,0]','[16777216,0]');")
            self.assertEqual([[1,'[0,0]',0.0],[2,'[3,4]',0.0]],list(execute(db,
                "SELECT id,s,L2_DISTANCE(v,'[16777216,0]') AS distance FROM t ORDER BY distance LIMIT 2;")[1]))
            execute(db,"UPDATE t SET v='[3,4]' WHERE id=1;")
            self.assertEqual([[5.0]],list(execute(db,"SELECT L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE id=1;")[1]))

    def test_quoted_invalid_vectors_fail_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(directory)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db,'BEGIN;')
            execute(db,"INSERT INTO t VALUES (1,'[0,0]');")
            for literal in ('[1]','[NaN,0]','[Infinity,0]','[1e100,0]',
                            '[null,0]','[true,0]','[[1],0]','[1,','null'):
                with self.assertRaises(DBError,msg=literal):
                    execute(db,"INSERT INTO t VALUES (2,'[0,0]'),(3,'%s');"%literal)
                self.assertEqual([[1]],list(execute(db,'SELECT id FROM t;')[1]))
            execute(db,'COMMIT;')
            db.close()
            db=Database(directory)
            self.assertEqual([[1]],list(execute(db,'SELECT id FROM t;')[1]))


class PublicAssertionReader(unittest.TestCase):
    def test_empty_assertion_still_requires_success(self):
        with self.assertRaises(AssertionError):
            check({'error':'TYPE'}, {})

    def test_unordered_comparison_keeps_duplicate_multiplicity(self):
        check({'rows':[[2],[1],[1]]},{'rows':[[1],[2],[1]],'unordered':True})
        with self.assertRaises(AssertionError):
            check({'rows':[[1],[2]]},{'rows':[[1],[1]],'unordered':True})

    def test_bigint_is_not_compared_as_float(self):
        check({'rows':[[9007199254740993]]},{'rows':[[9007199254740993]]})
        with self.assertRaises(AssertionError):
            check({'rows':[[float(9007199254740993)]]},{'rows':[[9007199254740993]]})

    def test_order_columns_and_affected_rows_are_checked(self):
        for actual,expected in [({'columns':['b','a']},{'columns':['a','b']}),
                                ({'affected_rows':1},{'affected_rows':2}),
                                ({'rows':[[2],[1]]},{'rows':[[1],[2]]})]:
            with self.assertRaises(AssertionError):
                check(actual,expected)


if __name__=='__main__':unittest.main()
