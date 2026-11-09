import subprocess
import sys
import tempfile
import unittest


class CliTest(unittest.TestCase):
    def test_streaming_protocol_restart_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            cmd = [sys.executable, "-m", "minivectordb", "--data", directory]
            sql = "CREATE TABLE t (id BIGINT PRIMARY KEY, text VARCHAR(20)); INSERT INTO t VALUES (1,'a;b'); BEGIN; INSERT INTO t VALUES (2,'pending');"
            first = subprocess.run(cmd, input=sql, text=True, capture_output=True, check=True)
            self.assertTrue(first.stdout.startswith("READY\nOK\t0\nOK\t1\n"))
            second = subprocess.run(cmd, input="SELECT * FROM t;", text=True, capture_output=True, check=True)
            self.assertIn('ROW\t1\t"a;b"\nEND\n', second.stdout)
            self.assertNotIn('pending', second.stdout)


if __name__ == "__main__":
    unittest.main()
