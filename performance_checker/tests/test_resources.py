import unittest

from performance_checker.resources import io_delta, parse_io_stat, parse_linux_device_number, parse_memory_peak


class ResourceParserTests(unittest.TestCase):
    def test_linux_device_number(self):
        self.assertEqual(parse_linux_device_number("2096\n"), "8:48")
        for value in ("", "-1", "invalid", str(1 << 64)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_linux_device_number(value)

    def test_device_counters_and_delta(self):
        before = parse_io_stat("8:0 rbytes=10 wbytes=20 rios=2 wios=3\n8:16 rbytes=5 wbytes=6\n")
        after = parse_io_stat("8:0 wbytes=35 rbytes=14 rios=4\n8:16 rbytes=100 wbytes=200\n")
        self.assertEqual(before["8:0"], {"rbytes": 10, "wbytes": 20})
        self.assertEqual(io_delta(before, after, "8:0"), 19)

    def test_bad_io_snapshots_are_not_zero(self):
        cases = ("", " \n", "8:0 rbytes=1", "8:0 wbytes=1", "8:0 rbytes=-1 wbytes=1",
                 "8:0 rbytes=1 wbytes=2 rbytes=3", "8:0 rbytes=1 wbytes=2\n8:0 rbytes=1 wbytes=2",
                 "8:0 rbytes=1 wbytes=2 extra", "8:x rbytes=1 wbytes=2",
                 "8:0 rbytes=1 wbytes=2 rios=oops")
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                parse_io_stat(case)

    def test_delta_requires_device_and_monotone_counters(self):
        before = parse_io_stat("8:0 rbytes=10 wbytes=20")
        after = parse_io_stat("8:0 rbytes=9 wbytes=30")
        for device in ("", None, "8:1", "8:0"):
            with self.subTest(device=device), self.assertRaises(ValueError):
                io_delta(before, after, device)

    def test_memory_peak(self):
        self.assertEqual(parse_memory_peak("12345\n"), 12345)
        self.assertEqual(parse_memory_peak("0"), 0)
        for value in ("", "max", "-1", "1 2", "1.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_memory_peak(value)


if __name__ == "__main__":
    unittest.main()
