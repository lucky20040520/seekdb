import random
import tempfile
import unittest

from minivectordb.external_sort import sort_rows


class ExternalSortTest(unittest.TestCase):
    def test_multiple_merge_passes_and_primary_key_ties(self):
        rows = [{"id": i, "a": i % 5, "b": i % 3} for i in range(300)]
        random.Random(42).shuffle(rows)
        ordering = [(lambda row: row["a"], True), (lambda row: row["b"], False)]
        expected = sorted(rows, key=lambda row: (-row["a"], row["b"], row["id"]))
        with tempfile.TemporaryDirectory() as directory:
            actual = list(sort_rows(iter(rows), ordering, "id", directory, run_bytes=100, fan_in=3))
            self.assertEqual(actual, expected)
            top = list(sort_rows(iter(rows), ordering, "id", directory, limit=17, run_bytes=100, fan_in=3))
            self.assertEqual(top, expected[:17])


if __name__ == "__main__":
    unittest.main()
