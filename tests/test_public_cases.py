"""Run the official public examples against the in-process SQL API.

The examples specify result semantics, not a process transport protocol.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minivectordb.sql import execute
from minivectordb.storage import Database


ROOT = Path(__file__).resolve().parent.parent / "mysqltest"


def same_value(actual, expected):
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            same_value(a, e) for a, e in zip(actual, expected)
        )
    if isinstance(expected, float):
        return type(actual) in (float, int) and math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-5)
    return type(actual) is type(expected) and actual == expected


def same_unordered(actual, expected):
    if len(actual) != len(expected):
        return False
    # A maximum matching preserves duplicate counts and handles float tolerance
    # without relying on the order in which equivalent rows are encountered.
    matches = [-1] * len(actual)

    def augment(expected_index, visited):
        for i, row in enumerate(actual):
            if i not in visited and same_value(row, expected[expected_index]):
                visited.add(i)
                if matches[i] == -1 or augment(matches[i], visited):
                    matches[i] = expected_index
                    return True
        return False

    return all(augment(i, set()) for i in range(len(expected)))


class PublicCaseTest(unittest.TestCase):
    def test_result_comparison_rules(self):
        self.assertTrue(same_value([[1, 1.000001]], [[1, 1.0]]))
        self.assertFalse(same_value([[1.0]], [[1]]))
        self.assertTrue(same_unordered([[2], [1], [1]], [[1], [2], [1]]))
        self.assertFalse(same_unordered([[2], [2], [1]], [[1], [2], [1]]))

    def test_official_public_examples(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        for case in manifest["cases"]:
            name = case["id"]
            statements = [line for line in (ROOT / "cases" / (name + ".test")).read_text(encoding="utf-8").splitlines() if line.strip()]
            expected = json.loads((ROOT / "cases" / (name + ".result")).read_text(encoding="utf-8"))
            self.assertEqual(len(statements), case["statements"], name)
            self.assertEqual(len(expected), len(statements), name)
            with tempfile.TemporaryDirectory() as directory:
                db = Database(directory)
                try:
                    for index, (statement, assertion) in enumerate(zip(statements, expected), 1):
                        with self.subTest(case=name, statement=index, sql=statement):
                            result = execute(db, statement)
                            if "affected_rows" in assertion:
                                self.assertEqual(result.affected, assertion["affected_rows"])
                            if "columns" in assertion:
                                self.assertEqual(result.columns, assertion["columns"])
                            if "rows" in assertion:
                                compare = same_unordered if assertion.get("unordered") else same_value
                                self.assertTrue(compare(result.rows, assertion["rows"]),
                                                "actual rows: {!r}; expected rows: {!r}".format(result.rows, assertion["rows"]))
                finally:
                    db.close()

    def test_public_examples_through_local_process(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        for case in manifest["cases"]:
            name = case["id"]
            statements = (ROOT / "cases" / (name + ".test")).read_text(encoding="utf-8")
            expected = json.loads((ROOT / "cases" / (name + ".result")).read_text(encoding="utf-8"))
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, PYTHONUTF8="1")
                process = subprocess.run([sys.executable, "-m", "minivectordb", "--data", directory],
                                         input=statements, capture_output=True, text=True,
                                         encoding="utf-8", env=env, timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr)
                lines = iter(process.stdout.splitlines())
                self.assertEqual(next(lines), "READY")
                for assertion in expected:
                    first = next(lines)
                    self.assertFalse(first.startswith("ERROR\t"), first)
                    if first.startswith("OK\t"):
                        actual = {"affected_rows": int(first.split("\t", 1)[1])}
                    else:
                        self.assertTrue(first.startswith("COLUMNS\t"), first)
                        actual = {"columns": first.split("\t")[1:], "rows": []}
                        for line in lines:
                            if line == "END":
                                break
                            self.assertTrue(line.startswith("ROW\t"), line)
                            actual["rows"].append([json.loads(value) for value in line.split("\t")[1:]])
                        else:
                            self.fail("missing END marker")
                    if "affected_rows" in assertion:
                        self.assertEqual(actual.get("affected_rows"), assertion["affected_rows"])
                    if "columns" in assertion:
                        self.assertEqual(actual.get("columns"), assertion["columns"])
                    if "rows" in assertion:
                        compare = same_unordered if assertion.get("unordered") else same_value
                        self.assertTrue(compare(actual.get("rows"), assertion["rows"]))
                self.assertEqual(list(lines), [])


if __name__ == "__main__":
    unittest.main()
