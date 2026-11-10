import random
import tempfile
import unittest

from minivectordb.sql import execute
from minivectordb.storage import Database
from minivectordb.ann import ProjectionIndex


class AnnTest(unittest.TestCase):
    def test_probe_order_favors_nearby_boundaries(self):
        index = ProjectionIndex.__new__(ProjectionIndex)
        index.BITS = 2
        index.planes = [[1, 0], [0, 1]]
        # The second boundary is much closer than the first one.
        self.assertEqual(index.probe_order([10, 1]), [3, 1, 2, 0])

    def test_sql_ann_budget_and_selective_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            try:
                execute(db, "CREATE TABLE docs (id BIGINT PRIMARY KEY, category BIGINT, v VECTOR(4))")
                rng = random.Random(123)
                db.insert("docs", ({"id": i, "category": 1 if i < 3 else 0,
                                    "v": [rng.uniform(-1, 1) for _ in range(4)]} for i in range(600)))
                execute(db, "CREATE VECTOR INDEX vec ON docs(v)")
                db._build_vector_index("vec")
                original_get = db.get
                reads = [0]
                def counted_get(table, key):
                    reads[0] += 1
                    return original_get(table, key)
                db.get = counted_get
                query = "L2_DISTANCE(v, '[0.1,0.2,-0.1,0.5]')"
                ann = execute(db, "SELECT id FROM docs ORDER BY " + query + " LIMIT 10", search_mode="ann").rows
                self.assertEqual(len(ann), 10)
                self.assertEqual(len({row[0] for row in ann}), 10)
                self.assertGreaterEqual(reads[0], 558)
                self.assertLess(reads[0], 600)
                reads[0] = 0
                sql = "SELECT id FROM docs WHERE category = 1 ORDER BY " + query + " LIMIT 10"
                filtered = execute(db, sql, search_mode="ann").rows
                self.assertEqual(reads[0], 600)
                self.assertEqual(filtered, execute(db, sql, search_mode="exact").rows)
            finally:
                db.close()

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
