"""Run the AI COLUMN mysqltests on a local instance.

The supplied SQL, expected results, mock service and request assertions are
used directly. The instance must be dedicated to these tests.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys


CASES = (
    "ai_column_ddl", "ai_column_t1_dml", "ai_column_worker",
    "ai_column_retry_strict", "ai_column_vector_index",
)


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
        command = [
            args.mysqltest, "--no-defaults", "--host=127.0.0.1",
            "--port=" + str(args.port), "--user=root", "--database=test",
            "--protocol=tcp", "--default-character-set=utf8mb4",
            "--test-file=" + str(suite / "t" / (name + ".test")),
            "--result-file=" + str(expected),
            "--logdir=" + str(case_dir), "--tmpdir=" + str(case_dir),
        ]
        # The vector result explicitly contains EXPLAIN output; the other
        # supplied cases do not include plans in their expected output.
        if not vector:
            command.append("--disable-explain")
        try:
            result = subprocess.run(command, cwd=repo / "tools/deploy", text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
            output, code = result.stdout, result.returncode
        except subprocess.TimeoutExpired as exc:
            output, code = str(exc), 124
        finally:
            for port in range(19120, 19125):
                subprocess.run([sys.executable, str(suite / "include/ai_column_mock.py"),
                                "stop", str(port)], check=False, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (case_dir / "mysqltest.log").write_text(output, encoding="utf-8")
        results[name] = {"exit_code": code, "passed": code == 0}
        print(name + ": " + ("PASS" if code == 0 else "FAIL"), flush=True)
        if code:
            print(output[-8000:], flush=True)
    (artifacts / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return int(any(not item["passed"] for item in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
