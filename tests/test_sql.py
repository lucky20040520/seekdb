import tempfile
import unittest

from minivectordb.sql import execute
from minivectordb.storage import Database, DatabaseError


class SqlTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(self.directory.name)
        execute(self.db, "CREATE TABLE docs (id BIGINT PRIMARY KEY, title VARCHAR(8), embedding VECTOR(2))")

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_sql_vector_filter_ties_and_transactions(self):
        execute(self.db, "INSERT INTO docs VALUES (3,'other','[0,0]'), (2,'中文','[1,0]'), (1,'中文','[-1,0]')")
        result = execute(self.db, "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS d FROM docs WHERE title = '中文' ORDER BY d LIMIT 2")
        self.assertEqual(result.columns, ["id", "d"])
        self.assertEqual(result.rows, [[1, 1.0], [2, 1.0]])
        self.assertEqual(execute(self.db, "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 0").rows, [])
        execute(self.db, "BEGIN")
        execute(self.db, "UPDATE docs SET embedding = '[0,0]' WHERE id = 2")
        execute(self.db, "DELETE FROM docs WHERE id = 1")
        self.assertEqual(execute(self.db, "SELECT id FROM docs WHERE title = '中文' ORDER BY L2_DISTANCE(embedding, '[0,0]') LIMIT 2").rows, [[2]])
        execute(self.db, "ROLLBACK")
        self.assertEqual(execute(self.db, "SELECT id FROM docs WHERE title = '中文' ORDER BY id DESC").rows, [[2], [1]])
        self.assertEqual(execute(self.db, "SELECT id FROM docs ORDER BY title DESC, id ASC LIMIT 2").rows, [[1], [2]])

    def test_failed_statement_is_atomic_and_syntax_not_silently_accepted(self):
        execute(self.db, "BEGIN")
        execute(self.db, "INSERT INTO docs VALUES (1,'ok','[1,2]')")
        with self.assertRaises(DatabaseError):
            execute(self.db, "INSERT INTO docs VALUES (2,'ok','[1,2]'), (1,'dup','[1,2]')")
        with self.assertRaises(DatabaseError):
            execute(self.db, "DELETE FROM docs WHERE id = 1 OR id = 2")
        execute(self.db, "COMMIT")
        self.db.close()
        self.db = Database(self.directory.name)
        self.assertEqual(execute(self.db, "SELECT id FROM docs").rows, [[1]])

    def test_identifiers_strings_and_validation_on_empty_table(self):
        with self.assertRaises(DatabaseError):
            execute(self.db, "UPDATE docs SET embedding = '[1]' WHERE id = 999")
        execute(self.db, "INSERT INTO docs (title, embedding, id) VALUES ('it''s ok','[1,2]',5)")
        self.assertEqual(execute(self.db, "SELECT title FROM `docs` WHERE id >= 5;").rows, [["it's ok"]])
        with self.assertRaises(DatabaseError):
            execute(self.db, "SELECT missing FROM docs WHERE id = 999")
        with self.assertRaises(DatabaseError):
            execute(self.db, "SELECT id FROM docs WHERE title = 1")

    def test_streamed_results_match_materialized_results(self):
        execute(self.db, "INSERT INTO docs VALUES (1,'a','[1,0]'), (2,'b','[2,0]'), (3,'b','[3,0]')")
        for sql in ("SELECT id FROM docs", "SELECT id FROM docs ORDER BY title DESC, id ASC", "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[0,0]') LIMIT 2"):
            expected = execute(self.db, sql)
            result = execute(self.db, sql, stream=True)
            self.assertFalse(isinstance(result.rows, list))
            self.assertEqual(list(result.rows), expected.rows)


if __name__ == "__main__":
    unittest.main()
