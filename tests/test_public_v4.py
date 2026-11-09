"""Run the pinned v4 examples; directives are handled by this local driver."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from minivectordb.sql import execute
from minivectordb.storage import Database, DatabaseError
from test_public_cases import same_unordered, same_value


ROOT = Path(__file__).resolve().parent.parent / "mysqltest_v4"


def cases():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        lines = [line.strip() for line in (ROOT / case["test"]).read_text(encoding="utf-8").splitlines() if line.strip()]
        expected = json.loads((ROOT / case["result"]).read_text(encoding="utf-8"))
        sql_count = sum(not line.startswith("--") for line in lines)
        if sql_count != case["statements"] or sql_count != len(expected):
            raise AssertionError("statement count mismatch: " + case["id"])
        assertions = iter(expected)
        events = [(line, None if line.startswith("--") else next(assertions)) for line in lines]
        yield case["id"], events


class PublicV4Test(unittest.TestCase):
    def check_result(self, actual, expected):
        if "error" in expected:
            self.assertEqual(actual.get("error"), expected["error"])
            return
        self.assertNotIn("error", actual, repr(actual))
        for field in ("affected_rows", "columns"):
            if field in expected:
                self.assertEqual(actual.get(field), expected[field])
        if "rows" in expected:
            compare = same_unordered if expected.get("unordered") else same_value
            self.assertTrue(compare(actual.get("rows"), expected["rows"]),
                            "actual: {!r}; expected: {!r}".format(actual.get("rows"), expected["rows"]))

    def test_v4_in_process(self):
        for name, events in cases():
            with tempfile.TemporaryDirectory() as directory:
                db = Database(directory)
                mode = "exact"
                try:
                    for line, expected in events:
                        with self.subTest(case=name, line=line):
                            if line == "--restart":
                                db.close()
                                db = Database(directory)
                            elif line == "--mode ann":
                                mode = "ann"
                            elif line.startswith("--"):
                                self.fail("unsupported driver directive: " + line)
                            else:
                                try:
                                    result = execute(db, line, search_mode=mode)
                                    actual = {"affected_rows": result.affected, "columns": result.columns, "rows": result.rows}
                                except DatabaseError as exc:
                                    actual = {"error": exc.code}
                                self.check_result(actual, expected)
                finally:
                    db.close()

    def test_v4_local_process(self):
        for name, events in cases():
            segments = [[]]
            for event in events:
                if event[0] == "--restart":
                    segments.append([])
                else:
                    segments[-1].append(event)
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                mode = "exact"
                for segment in segments:
                    cmd = [sys.executable, "-m", "minivectordb", "--data", directory, "--search-mode", mode]
                    process = subprocess.run(cmd, input="\n".join(line for line, _ in segment) + "\n",
                                             capture_output=True, text=True, encoding="utf-8",
                                             env=dict(os.environ, PYTHONUTF8="1"), timeout=60)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    output = iter(process.stdout.splitlines())
                    self.assertEqual(next(output), "READY")
                    for line, expected in segment:
                        first = next(output)
                        if line == "--mode ann":
                            self.assertEqual(first, "MODE\tann")
                            mode = "ann"
                            continue
                        if line.startswith("--"):
                            self.fail("unsupported driver directive: " + line)
                        if first.startswith("ERROR\t"):
                            fields = first.split("\t", 2)
                            self.assertGreaterEqual(len(fields), 2)
                            self.assertTrue(fields[1])
                            actual = {"error": fields[1]}
                        elif first.startswith("OK\t"):
                            actual = {"affected_rows": int(first.split("\t", 1)[1])}
                        else:
                            self.assertTrue(first.startswith("COLUMNS\t"), first)
                            actual = {"columns": first.split("\t")[1:], "rows": []}
                            for row in output:
                                if row == "END":
                                    break
                                self.assertTrue(row.startswith("ROW\t"), row)
                                actual["rows"].append([json.loads(value) for value in row.split("\t")[1:]])
                            else:
                                self.fail("missing result terminator")
                        self.check_result(actual, expected)
                    self.assertEqual(list(output), [])


if __name__ == "__main__":
    unittest.main()
