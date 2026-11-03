"""Run the competition's unmodified AI COLUMN mysqltests on a local instance.

The supplied SQL, expected results, mock service and request assertions are
used directly. The instance must be dedicated to these tests.
"""

import argparse
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys


CASES = (
    "ai_column_ddl", "ai_column_t1_dml", "ai_column_worker",
    "ai_column_retry_strict", "ai_column_vector_index",
)


def normalize_plan_estimates(text):
    """Keep the actual plan; estimates depend on local optimizer statistics."""
    in_plan = False
    lines = []
    for line in text.splitlines(keepends=True):
        if "|EST.ROWS|EST.TIME(us)|" in line:
            in_plan = True
        elif in_plan and line.startswith("Outputs & filters:"):
            in_plan = False
        if in_plan:
            parts = line.split("|")
            if (len(parts) == 7 and parts[1].strip().isdigit()
                    and all(re.fullmatch(r"[0-9.eE+\-]+", parts[i].strip()) for i in (4, 5))):
                parts[4], parts[5] = "<estimate>", "<cost>"
                line = "|".join(parts)
        lines.append(line)
    return "".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysqltest", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    suite = repo / "tools/deploy/mysql_test/test_suite/ai_column"
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in args.case or CASES:
        case_dir = artifacts / name
        case_dir.mkdir(exist_ok=True)
        expected = suite / "r/mysql" / (name + ".result")
        vector = name == "ai_column_vector_index"
        actual = case_dir / (name + ".actual.result")
        command = [
            args.mysqltest, "--no-defaults", "--host=127.0.0.1",
            "--port=" + str(args.port), "--user=root", "--database=test",
            "--protocol=tcp", "--default-character-set=utf8mb4",
            "--test-file=" + str(suite / "t" / (name + ".test")),
            "--result-file=" + str(actual if vector else expected),
            "--logdir=" + str(case_dir), "--tmpdir=" + str(case_dir),
        ]
        # The vector result explicitly contains EXPLAIN output; the other
        # supplied cases do not include plans in their expected output.
        if vector:
            # Record into the artifact directory, never into the supplied
            # expected result. Compare every line below, retaining the plan
            # operator, index, access columns, filters and actual SQL results.
            command.append("--record")
        else:
            command.append("--disable-explain")
        try:
            result = subprocess.run(command, cwd=repo / "tools/deploy", text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
            output, code = result.stdout, result.returncode
            if vector and code == 0:
                expected_text = expected.read_text(encoding="utf-8")
                actual_text = actual.read_text(encoding="utf-8")
                expected_normalized = normalize_plan_estimates(expected_text)
                actual_normalized = normalize_plan_estimates(actual_text)
                (case_dir / "expected.normalized.result").write_text(expected_normalized, encoding="utf-8")
                (case_dir / "actual.normalized.result").write_text(actual_normalized, encoding="utf-8")
                if expected_normalized != actual_normalized:
                    code = 1
                    output += "".join(difflib.unified_diff(
                        expected_normalized.splitlines(True), actual_normalized.splitlines(True),
                        fromfile=str(expected), tofile=str(actual)))
        except subprocess.TimeoutExpired as exc:
            output, code = str(exc), 124
        finally:
            for port in range(19120, 19125):
                subprocess.run([sys.executable, str(suite / "include/ai_column_mock.py"),
                                "stop", str(port)], check=False, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (case_dir / "mysqltest.log").write_text(output, encoding="utf-8")
        results[name] = {"exit_code": code, "passed": code == 0}
        if vector:
            results[name]["normalized_fields"] = ["EXPLAIN EST.ROWS", "EXPLAIN EST.TIME(us)"]
        print(name + ": " + ("PASS" if code == 0 else "FAIL"), flush=True)
        if code:
            print(output[-8000:], flush=True)
    (artifacts / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return int(any(not item["passed"] for item in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
