import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from performance_checker.checker import baseline_medians, calculate, main, workload_phase


def measurements():
    metric = {"time_ms": 10, "io_bytes": 1000, "memory_bytes": 10000}
    checks = {"write_correct": True, "query_correct": True, "resources_ok": True,
              "recall_unfiltered": 1.0, "recall_filtered": 1.0}
    return [{"write": dict(metric), "query": dict(metric), "checks": dict(checks)} for _ in range(3)]


class CheckerTests(unittest.TestCase):
    def test_baseline_medians_are_per_metric(self):
        values = measurements()
        for run, ms in zip(values, [10, 50, 20]):
            run["write"]["time_ms"] = ms
        self.assertEqual(baseline_medians({"rounds": values})["write"]["time_ms"], 20)
        with self.assertRaises(ValueError):
            baseline_medians({"rounds": values[:2]})

    def test_missing_baseline_or_io_never_creates_an_indicator(self):
        rounds = measurements()
        self.assertFalse(calculate(None, rounds)["available"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(json.dumps({"rounds": rounds}), encoding="utf-8")
            self.assertEqual(calculate(path, rounds)["median_efficiency"], 0.5)
            rounds[0]["query"]["io_bytes"] = None
            self.assertFalse(calculate(path, rounds)["available"])

    def test_import_and_help_do_not_start_docker(self):
        with patch("performance_checker.checker.docker", side_effect=AssertionError("unexpected Docker")):
            with self.assertRaises(SystemExit) as exited:
                main(["--help"])
            self.assertEqual(exited.exception.code, 0)

    def test_query_phase_uses_separate_validation_and_checks_each_answer(self):
        class Fake:
            def __init__(self, image, volume, label, args):
                self.requests = []
            def start(self):
                return 1
            def request(self, sql, mode="exact"):
                self.requests.append((sql, mode))
                if sql.startswith("INSERT"):
                    return {"ok": True, "affected_rows": 10}
                if sql in ("q0", "q1"):
                    return {"ok": True, "columns": ["id", "distance"], "rows": [[1, 0.0]]}
                return {"ok": True}
            def counters(self):
                return {"io_bytes": 8, "memory_bytes": 16, "raw": {"sync_exit_code": 0}}
            def close(self):
                pass
            def cleanup(self):
                pass
        class Args:
            dimensions = 2
            create_index = True
            mode = "ann"
            k = 1
        workload = {"records": [[i, "tag1", [0, 0]] for i in range(10)], "batches": ["INSERT batch"],
                    "requests": [{"sql": "q0", "tag": None, "query": [0, 0]},
                                 {"sql": "q1", "tag": "tag1", "query": [0, 0]}]}
        with patch("performance_checker.checker.Container", Fake), \
             patch("performance_checker.checker.docker") as docker, \
             patch("performance_checker.checker.verify_persistence", return_value=True) as verify, \
             patch("performance_checker.checker.verify_answer", return_value=1.0) as answer:
            result = workload_phase("image", workload, Args(), "query")
        self.assertEqual(result["query_counts"], {"unfiltered": 1, "filtered": 1})
        self.assertEqual(result["recall"], {"unfiltered": 1.0, "filtered": 1.0})
        self.assertEqual(answer.call_count, 2)
        verify.assert_called_once()
        self.assertEqual(docker.call_args_list[0].args[:2], ("volume", "create"))
        self.assertEqual(docker.call_args_list[-1].args[:2], ("volume", "rm"))


if __name__ == "__main__":
    unittest.main()
