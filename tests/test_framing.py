import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from minivector.__main__ import statements


class InputFraming(unittest.TestCase):
    def test_utf8_pipe_overrides_non_utf8_host_default(self):
        with tempfile.TemporaryDirectory() as directory:
            text = '\u4e2d\u6587\U0001f600'
            sql = ("CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(3));"
                   "INSERT INTO t VALUES (1,'" + text + "');SELECT s FROM t;")
            result = subprocess.run(
                [sys.executable, '-u', '-m', 'minivector', '--data-dir', directory],
                input=sql, capture_output=True, text=True, encoding='utf-8', timeout=15,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=dict(os.environ, PYTHONIOENCODING='cp1252'))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(['LOCAL_READY', 'OK 0', 'OK 1', 's',
                              json.dumps(text, ensure_ascii=False), 'END'],
                             result.stdout.splitlines())

    def test_request_without_newline_gets_response(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, '-u', '-m', 'minivector', '--data-dir', directory],
                cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8',
                env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            output = queue.Queue()
            def consume():
                for line in process.stdout:
                    output.put(line.rstrip('\n'))
            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            try:
                self.assertEqual('LOCAL_READY', output.get(timeout=5))
                process.stdin.write('CREATE TABLE t(id BIGINT PRIMARY KEY);')
                process.stdin.flush()
                try:
                    response = output.get(timeout=2)
                except queue.Empty:
                    self.fail('SQL terminator received, but response waits for newline')
                self.assertEqual('OK 0', response)
                self.assertIsNone(process.poll())
                process.stdin.write('INSERT INTO t VALUES (1);')
                process.stdin.flush()
                self.assertEqual('OK 1', output.get(timeout=2))
                process.stdin.write('SELECT id FROM t;')
                process.stdin.flush()
                self.assertEqual(['id', '1', 'END'], [output.get(timeout=2) for _ in range(3)])
            finally:
                process.kill()
                process.wait(timeout=10)
                reader.join(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_quoted_and_commented_semicolons(self):
        parts = ["INSERT INTO `a;b` VALUES ('a;''b');",
                 "-- ignored ;\nSELECT 'multiline\n;value';",
                 "/* ; **/SELECT 'x';",
                 " SELECT 1"]
        self.assertEqual(parts, list(statements(io.StringIO(''.join(parts)))))


if __name__ == '__main__':
    unittest.main()
