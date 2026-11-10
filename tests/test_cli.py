import subprocess
import json
import os
import sys
import tempfile
import unittest


class CliTest(unittest.TestCase):
    def test_streaming_protocol_restart_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            cmd = [sys.executable, "-m", "minivectordb", "--data", directory]
            def start():
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"))
                self.assertEqual(json.loads(proc.stdout.readline()), {"type": "ready"})
                return proc
            def request(proc, ident, op, **fields):
                proc.stdin.write(json.dumps(dict(id=ident, op=op, **fields), ensure_ascii=False) + "\n")
                proc.stdin.flush()
                response = json.loads(proc.stdout.readline())
                self.assertEqual(response["id"], ident)
                self.assertIs(type(response["id"]), type(ident))
                return response
            proc = start()
            for i, sql in enumerate(("CREATE TABLE t (id BIGINT PRIMARY KEY, text VARCHAR(20));",
                                     "INSERT INTO t VALUES (1,'a;b');", "BEGIN;",
                                     "INSERT INTO t VALUES (2,'pending');")):
                self.assertIs(request(proc, i, "execute", sql=sql, search_mode="exact")["ok"], True)
            error = request(proc, "error", "execute", sql="INSERT INTO t VALUES (1,'duplicate');", search_mode="ann")
            self.assertIs(error["ok"], False)
            self.assertEqual(error["error"]["code"], "duplicate_key")
            error = request(proc, "overflow", "execute", sql="INSERT INTO t VALUES (" + "9" * 5000 + ",'x');", search_mode="exact")
            self.assertIs(error["ok"], False)
            self.assertTrue(error["error"]["code"])
            self.assertEqual(request(proc, "after", "execute", sql="SELECT id FROM t ORDER BY id;", search_mode="exact")["rows"], [[1], [2]])
            self.assertEqual(request(proc, "bye", "shutdown"), {"id": "bye", "ok": True})
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0)
            self.assertEqual(proc.stdout.read(), "")
            proc.stdout.close()
            proc.stderr.close()
            proc = start()
            result = request(proc, 999999999999999999, "execute", sql="SELECT * FROM t;", search_mode="exact")
            self.assertEqual(result["rows"], [[1, "a;b"]])
            self.assertEqual(request(proc, 0, "shutdown"), {"id": 0, "ok": True})
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
