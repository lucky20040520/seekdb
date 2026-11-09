import random
import tempfile
import unittest

from minivectordb.sql import execute
from minivectordb.storage import Database


class AnnTest(unittest.TestCase):
    def test_index_maintenance_filter_transaction_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db, "CREATE TABLE docs (id BIGINT PRIMARY KEY, category BIGINT, v VECTOR(4))")
            rng = random.Random(12)
            db.insert("docs", ({"id": i, "category": i % 2, "v": [rng.uniform(-1, 1) for _ in range(4)]} for i in range(120)))
            execute(db, "CREATE VECTOR INDEX docs_vec ON docs(v)")
            sql = "SELECT id, L2_DISTANCE(v, '[0.1,0.2,-0.1,0.5]') AS d FROM docs WHERE category = 1 ORDER BY d LIMIT 10"
            self.assertEqual(execute(db, sql, search_mode="ann").rows, execute(db, sql).rows)
            filt = lambda row: row["category"] == 1
            query = [0.1, 0.2, -0.1, 0.5]
            self.assertEqual(db.search("docs", "v", query, 10, filt, mode="ann"), db.search("docs", "v", query, 10, filt))
            self.assertEqual(len(db.search("docs", "v", query, 10, filt, mode="ann", candidate_budget=10)), 10)
            db.begin()
            db.update("docs", lambda row: row["id"] == 1, {"v": query})
            self.assertEqual(db.search("docs", "v", query, 10, filt, mode="ann")[0][1], 1)
            db.rollback()
            db.update("docs", lambda row: row["id"] == 1, {"v": query})
            self.assertEqual(db.search("docs", "v", query, 10, filt, mode="ann")[0][1], 1)
            db.delete("docs", lambda row: row["id"] == 1)
            self.assertNotIn(1, [item[1] for item in db.search("docs", "v", query, 10, filt, mode="ann")])
            db.checkpoint()
            db.close()
            db = Database(directory)
            self.assertIn("docs_vec", db.vector_indexes)
            self.assertEqual(db.search("docs", "v", query, 10, filt, mode="ann"), db.search("docs", "v", query, 10, filt))
            db.close()


if __name__ == "__main__":
    unittest.main()
