import tempfile
import unittest

from minivectordb.page_data import PageData


class PageDataTest(unittest.TestCase):
    def test_cross_page_rows_and_cache_invalidation(self):
        schema = {"columns": {"id": ["BIGINT", None], "text": ["VARCHAR", 5000], "v": ["VECTOR", 2]}}
        with tempfile.TemporaryDirectory() as directory:
            data = PageData(directory, cache_pages=2)
            first = {"id": -2, "text": "中" * 1800, "v": [1.5, -2.0]}
            first_offset = data.append(schema, first)
            self.assertEqual(data.read(schema, first_offset), first)
            second = {"id": 3, "text": "hello", "v": [0.0, 4.0]}
            second_offset = data.append(schema, second)
            self.assertEqual(data.read(schema, second_offset), second)
            self.assertEqual(data.read(schema, first_offset), first)
            self.assertLessEqual(len(data.cache), 2)
            data.close()


if __name__ == "__main__":
    unittest.main()
