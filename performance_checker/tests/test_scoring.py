import copy
import math
import unittest

from performance_checker.scoring import _round_efficiency, calculate_report, rank_to_points


def fixtures():
    baseline = {
        "write": {"time_ms": 100, "io_bytes": 2**20, "memory_bytes": 4 * 2**20},
        "query": {"time_ms": 20, "io_bytes": 0, "memory_bytes": 2**20},
    }
    check = {"write_correct": True, "query_correct": True, "resources_ok": True,
             "recall_unfiltered": 0.95, "recall_filtered": 0.95}
    runs = [{"write": copy.deepcopy(baseline["write"]),
             "query": copy.deepcopy(baseline["query"]), "checks": dict(check)} for _ in range(3)]
    return baseline, runs


class ScoringTests(unittest.TestCase):
    def test_points_require_known_positive_rank(self):
        self.assertEqual(rank_to_points(1), 40)
        self.assertEqual(rank_to_points(2), 39.5)
        self.assertEqual(rank_to_points(80), 0.5)
        self.assertEqual(rank_to_points(81), 0)
        for bad in (0, -1, 1.5, True, None):
            with self.assertRaises(ValueError):
                rank_to_points(bad)

    def test_round_half_up(self):
        self.assertEqual(_round_efficiency(0.1234565), 0.123457)
        self.assertEqual(_round_efficiency(0.1234564), 0.123456)

    def test_equal_baseline_is_half(self):
        baseline, runs = fixtures()
        result = calculate_report(baseline, runs)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["median_efficiency"], 0.5)
        self.assertEqual(len(result["rounds"]), 3)

    def test_weights_epsilon_and_median(self):
        baseline, runs = fixtures()
        runs[0]["query"]["time_ms"] = 0
        runs[1]["query"]["time_ms"] = 40
        runs[2]["query"]["time_ms"] = 20
        # The third is the median, regardless of run ordering.
        self.assertEqual(calculate_report(baseline, runs)["median_efficiency"], 0.5)
        expected_query = .6 * (21 / 22) + .2 * .5 + .2 * .5
        expected_total = (.5 + 2 * expected_query) / 3
        self.assertAlmostEqual(calculate_report(baseline, runs)["rounds"][0]["efficiency"], expected_total)

    def test_ineligible_produces_no_metric(self):
        baseline, runs = fixtures()
        runs[1]["checks"]["recall_filtered"] = .949
        runs[2]["checks"]["write_correct"] = False
        report = calculate_report(baseline, runs)
        self.assertFalse(report["eligible"])
        self.assertIsNone(report["median_efficiency"])
        self.assertEqual(len(report["reasons"]), 2)

    def test_input_validation(self):
        baseline, runs = fixtures()
        for bad in (-1, math.inf, math.nan, True, "1", None):
            mutated = copy.deepcopy(baseline)
            mutated["write"]["io_bytes"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                calculate_report(mutated, runs)
        with self.assertRaises(ValueError):
            calculate_report({}, runs)
        with self.assertRaises(ValueError):
            calculate_report(baseline, runs[:2])
        runs[0]["checks"]["recall_filtered"] = 1.1
        with self.assertRaises(ValueError):
            calculate_report(baseline, runs)

    def test_large_values_do_not_overflow_sum(self):
        baseline, runs = fixtures()
        baseline["write"]["time_ms"] = 1e308
        for run in runs:
            run["write"]["time_ms"] = 1e308
        self.assertEqual(calculate_report(baseline, runs)["median_efficiency"], .5)


if __name__ == "__main__":
    unittest.main()
