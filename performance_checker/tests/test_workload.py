import unittest

from performance_checker.workload import make_workload, verify_answer, verify_records


class WorkloadTests(unittest.TestCase):
    def test_reproducible_batches_and_queries(self):
        first = make_workload(rows=103, dimensions=2, queries=4, k=3)
        self.assertEqual(first, make_workload(rows=103, dimensions=2, queries=4, k=3))
        self.assertEqual(len(first["batches"]), 2)
        self.assertIn("INSERT INTO docs (id, tag, v)", first["batches"][0])
        self.assertEqual([q["tag"] is None for q in first["requests"]], [True, False, True, False])
        self.assertIn("L2_DISTANCE(v,", first["requests"][0]["sql"])
        self.assertNotIn(" WHERE ", first["requests"][0]["sql"])
        self.assertIn(" WHERE ", first["requests"][1]["sql"])
        other = make_workload(rows=103, dimensions=2, queries=4, k=3, query_seed=3)
        self.assertEqual(first["records"], other["records"])
        self.assertNotEqual(first["requests"], other["requests"])

    def test_recall_and_valid_approximate_results(self):
        records = [[0, "a", [0.0, 0.0]], [1, "b", [1.0, 0.0]],
                   [2, "a", [2.0, 0.0]], [3, "a", [3.0, 0.0]]]
        self.assertEqual(verify_answer({"ok": True, "rows": [[0, 0], [2, 2]]}, records, [0, 0], None, 2), 0.5)
        self.assertEqual(verify_answer([[0, 0], [2, 2]], records, [0, 0], "a", 2), 1.0)
        self.assertEqual(verify_answer([], records, [0, 0], "absent", 2), 1.0)
        bad = ([[0, 0]], [[0, 0], [0, 0]], [[0, 0], [1, 7]], [[1, 1], [0, 0]], [[0, 0], [99, 1]])
        for result in bad:
            with self.subTest(result=result), self.assertRaises(ValueError):
                verify_answer(result, records, [0, 0], None, 2)
        with self.assertRaises(ValueError):
            verify_answer([[0, 0], [1, 1]], records, [0, 0], "a", 2)

    def test_all_records_checked(self):
        records = [[0, "a", [0.0, 1.0]], [1, "b", [2.0, 3.0]]]
        verify_records(list(reversed(records)), records)
        for actual in (records[:1], records + records[:1], [[0, "wrong", [0.0, 1.0]], records[1]],
                       [[0, "a", [0.0]], records[1]], [[0, "a", [0.0, 1.1]], records[1]]):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                verify_records(actual, records)

    def test_reject_bad_sizes(self):
        for kwargs in ({"rows": 0}, {"dimensions": 0}, {"queries": 0}, {"k": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                make_workload(**kwargs)


if __name__ == "__main__":
    unittest.main()
