"""Actual default process entry: official JSONL shapes, IDs and transaction modes."""
import json
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from minivector import jsonl
from minivector.engine import DBError


class JSONLContracts(unittest.TestCase):
    def test_bad_json_rejected_and_lazy_query_failure_never_sends_partial_result(self):
        for text in ('{"id":1,"id":2}', '{"id":NaN}', '{"id":1e999}'):
            with self.assertRaises(ValueError):
                jsonl.strict_json(text)
        with tempfile.TemporaryDirectory() as directory:
            class DB:
                path = directory
                def close(self):
                    pass
            def rows():
                yield ['x'*300000]  # force the staging buffer to spill to /data
                raise DBError('TEST', 'late query failure')
            output = io.StringIO()
            with patch.object(jsonl, 'execute', return_value=(['value'], rows())), \
                 patch.object(sys, 'stdin', io.StringIO('{"id":"q","op":"execute","sql":"SELECT x"}\n')), \
                 patch.object(sys, 'stdout', output):
                jsonl.serve(DB())
            messages = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([{'type':'ready'}, {'id':'q','ok':False,
                             'error':{'code':'TEST','message':'late query failure'}}], messages)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_default_entry_jsonl_modes_ids_errors_and_shutdown_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            for restart in range(2):
                process = subprocess.Popen([sys.executable, '-u', '-m', 'minivector', '--data-dir', directory],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', cwd=str(Path(__file__).resolve().parents[1]),
                    env=dict(os.environ, PYTHONIOENCODING='cp1252'))
                output = queue.Queue()
                def read():
                    for line in process.stdout:
                        output.put(line)
                thread = threading.Thread(target=read, daemon=True)
                thread.start()
                def receive():
                    return json.loads(output.get(timeout=10))
                def request(ident, sql=None, mode='exact', op='execute'):
                    value = {'id':ident, 'op':op}
                    if sql is not None:
                        value.update(sql=sql, search_mode=mode)
                    process.stdin.write(json.dumps(value, ensure_ascii=False)+'\n')
                    process.stdin.flush()
                    response = receive()
                    self.assertIs(type(response['id']), type(ident))
                    self.assertEqual(ident, response['id'])
                    self.assertIs(type(response['ok']), bool)
                    return response
                try:
                    self.assertEqual({'type':'ready'}, receive())
                    if not restart:
                        self.assertTrue(request('ddl', 'CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(8),v VECTOR(2));')['ok'])
                        self.assertEqual(1, request(9007199254740993, "INSERT INTO t VALUES(9007199254740993,'\u4e2d;\U0001f600','[3,4]');")['affected_rows'])
                        request('begin', 'BEGIN;')
                        request('change', "UPDATE t SET v='[0,0]';")
                        failure = request('dup', "INSERT INTO t VALUES(9007199254740993,'x','[1,0]');")
                        self.assertIs(False, failure['ok'])
                        self.assertTrue(failure['error']['code'])
                        for ident, mode in ((1, 'exact'), ('1', 'ann')):
                            self.assertEqual([[9007199254740993, 0.0]], request(ident,
                                "SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t ORDER BY d LIMIT 10;", mode)['rows'])
                    else:
                        response = request('restart', "SELECT s,L2_DISTANCE(v,'[0,0]') AS d FROM t;")
                        self.assertEqual([['\u4e2d;\U0001f600',5.0]], response['rows'])
                    self.assertEqual({'id':'stop','ok':True}, request('stop', op='shutdown'))
                    process.stdin.close()
                    self.assertEqual(0, process.wait(timeout=10))
                    thread.join(timeout=5)
                    self.assertTrue(output.empty(), 'extra stdout after shutdown')
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    thread.join(timeout=5)
                    for stream in (process.stdin,process.stdout,process.stderr):
                        stream.close()
