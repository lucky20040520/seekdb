import random
import tempfile
import unittest
from pathlib import Path

from minivectordb.page_index import PageIndex


class PageIndexTest(unittest.TestCase):
    def test_split_update_delete_reopen_and_bounded_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index"
            index = PageIndex(path, cache_pages=3, create=True)
            keys = list(range(-3000, 3000))
            random.Random(731).shuffle(keys)
            for key in keys:
                index.put(key, key + 3000)
            for key in range(-3000, 3000, 7):
                index.delete(key)
            for key in range(-3000, 3000, 14):
                index.put(key, 12345)
            expected = {key: (12345 if (key + 3000) % 14 == 0 else key + 3000)
                        for key in range(-3000, 3000) if (key + 3000) % 7 or (key + 3000) % 14 == 0}
            self.assertEqual(list(index.items()), sorted(expected.items()))
            self.assertLessEqual(len(index.cache), 3)
            index.close()
            index = PageIndex(path, cache_pages=2)
            self.assertEqual(list(index.items()), sorted(expected.items()))
            for key in (-3000, -2999, 0, 2999):
                self.assertEqual(index.get(key), expected.get(key))
            index.close()


if __name__ == "__main__":
    unittest.main()
