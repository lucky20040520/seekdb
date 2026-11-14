import subprocess
import sys
import time
import unittest
import queue

from performance_checker.transport import JsonlSession, ProtocolError


_CHILD = r'''
import json, sys
print('{"type":"ready"}', flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['op'] == 'shutdown':
        print(json.dumps({'id': request['id'], 'ok': True}), flush=True)
        break
    print(json.dumps({'id': request['id'], 'ok': request['sql'] != 'bad',
                      'rows': [[request['sql'], request['search_mode']]]}, ensure_ascii=False), flush=True)
'''


class TransportTests(unittest.TestCase):
    def spawn(self, source, **kwargs):
        process = subprocess.Popen([sys.executable, '-u', '-c', source],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup, process)
        return JsonlSession(process, startup_timeout=2, request_timeout=2, **kwargs)

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def test_unicode_failure_response_and_shutdown(self):
        session = self.spawn(_CHILD)
        self.assertEqual(session.wait_ready(), {"type": "ready"})
        result = session.execute("你好\n世界", mode="ann")
        self.assertEqual(result["rows"], [["你好\n世界", "ann"]])
        self.assertFalse(session.execute("bad")["ok"])
        self.assertTrue(session.shutdown()["ok"])

    def test_optional_client_timing_and_stderr_lines(self):
        lines = queue.Queue()
        source = _CHILD.replace("print('{\"type\":\"ready\"}', flush=True)",
                                "print('partial', end='', file=sys.stderr, flush=True)\n"
                                "print(' line', file=sys.stderr, flush=True)\n"
                                "print('{\"type\":\"ready\"}', flush=True)")
        session = self.spawn(source, stderr_line_handler=lines.put, measure_client=True)
        session.wait_ready()
        self.assertTrue(session.execute("SELECT 1")["ok"])
        self.assertEqual(lines.get(timeout=2), b"partial line")
        self.assertGreater(session.last_timing["roundtrip_ns"], 0)
        self.assertGreater(session.last_timing["encode_ns"], 0)
        self.assertGreater(session.last_timing["decode_ns"], 0)
        if hasattr(time, "thread_time_ns"):
            self.assertGreaterEqual(session.last_timing["main_cpu_ns"], 0)
            self.assertGreaterEqual(session.last_timing["writer_cpu_ns"], 0)
            self.assertGreaterEqual(session.last_timing["stdout_reader_cpu_ns"], 0)
        session.shutdown()

    def test_invalid_handshake(self):
        session = self.spawn("print('{\"type\":\"ready\",\"extra\":1}', flush=True)")
        with self.assertRaises(ProtocolError):
            session.wait_ready()

    def test_malformed_responses(self):
        for bad in ('{"id":"1","ok":true}', '{"id":true,"ok":true}',
                    '{"id":1,"ok":1}', '{"id":1,"ok":true,"x":NaN}',
                    '{"id":1,"ok":true,"x":1e999}', '{"id":1,"ok":true,"ok":true}'):
            source = "import sys\nprint('{\"type\":\"ready\"}',flush=True)\nsys.stdin.readline()\nprint(" + repr(bad) + ",flush=True)\n"
            with self.subTest(response=bad):
                session = self.spawn(source)
                session.wait_ready()
                with self.assertRaises(ProtocolError):
                    session.execute("SELECT 1")

    def test_response_limit(self):
        session = self.spawn("print('{\"type\":\"ready\",\"padding\":\"xxxxxxxxxxxx\"}',flush=True)", max_response_bytes=20)
        with self.assertRaises(ProtocolError):
            session.wait_ready()

    def test_response_timeout(self):
        session = self.spawn("import time\nprint('{\"type\":\"ready\"}',flush=True)\ntime.sleep(5)")
        session.wait_ready()
        session.request_timeout = .1
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            session.execute("SELECT 1")
        self.assertLess(time.monotonic() - start, 2)

    def test_write_timeout(self):
        session = self.spawn("import time\nprint('{\"type\":\"ready\"}',flush=True)\ntime.sleep(5)")
        session.wait_ready()
        session.request_timeout = .1
        with self.assertRaises(TimeoutError):
            session.execute("x" * (1024 * 1024))

    def test_shutdown_extra_stdout(self):
        source = "import json,sys\nprint('{\"type\":\"ready\"}',flush=True)\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'id':r['id'],'ok':True}),flush=True)\nprint('extra',flush=True)"
        session = self.spawn(source)
        session.wait_ready()
        with self.assertRaises(ProtocolError):
            session.shutdown()

    def test_unsolicited_message_before_request(self):
        source = "import time\nprint('{\"type\":\"ready\"}',flush=True)\nprint('{\"extra\":1}',flush=True)\ntime.sleep(5)"
        session = self.spawn(source)
        session.wait_ready()
        deadline = time.monotonic() + 1
        while session._messages.empty() and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaises(ProtocolError):
            session.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
