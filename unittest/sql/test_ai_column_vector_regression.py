"""Vector AI COLUMN regressions against a dedicated local seekdb instance.

Run with SEEKDB_TEST_PORT set. The HTTP fixture is local, deterministic, and
allows in-flight requests to be held while a newer row version is committed.
"""

import json
import os
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql


class EmbeddingMock(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), EmbeddingHandler)
        self.requests = []
        self.lock = threading.Lock()
        self.slow_started = threading.Event()
        self.release_slow = threading.Event()


class EmbeddingHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        values = body["input"]
        values = values if isinstance(values, list) else [values]
        with self.server.lock:
            self.server.requests.extend(values)
        if any(value.startswith("slow-") for value in values):
            self.server.slow_started.set()
            self.server.release_slow.wait(4)
        vectors = {"zero": 0, "ZERO": 10, "ZERO ": 0, "ten": 10, "twenty": 20, "slow-ten": 10}
        data = [{"index": i, "embedding": [vectors[value], 0, 0, 0]}
                for i, value in enumerate(values)]
        response = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(os.environ.get("SEEKDB_TEST_PORT"), "requires a dedicated seekdb instance")
class AiColumnVectorRegression(unittest.TestCase):
    @classmethod
    def connect(cls):
        return pymysql.connect(host="127.0.0.1", port=int(os.environ["SEEKDB_TEST_PORT"]),
                               user="root", autocommit=True, charset="utf8mb4", read_timeout=30)

    @classmethod
    def setUpClass(cls):
        cls.name = "ai_vec_reg_" + uuid.uuid4().hex[:10]
        cls.model = cls.name + "_model"
        cls.endpoint = cls.name + "_ep"
        cls.server = EmbeddingMock()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        with cls.connect() as db, db.cursor() as q:
            q.execute("CREATE DATABASE `" + cls.name + "`")
            q.execute("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(%s,%s)",
                      (cls.model, json.dumps({"type": "dense_embedding", "model_name": "test-embed"})))
            q.execute("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(%s,%s)",
                      (cls.endpoint, json.dumps({"ai_model_name": cls.model,
                       "url": "http://127.0.0.1:%d/v1/embeddings" % cls.server.server_port,
                       "access_key": "test-key", "provider": "openai", "request_model_name": "test-embed"})))

    @classmethod
    def tearDownClass(cls):
        cls.server.release_slow.set()
        try:
            with cls.connect() as db, db.cursor() as q:
                q.execute("DROP DATABASE `" + cls.name + "`")
                q.execute("CALL DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT(%s)", (cls.endpoint,))
                q.execute("CALL DBMS_AI_SERVICE.DROP_AI_MODEL(%s)", (cls.model,))
        finally:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join()

    def setUp(self):
        self.db = self.connect()
        self.q = self.db.cursor()
        self.q.execute("USE `" + self.name + "`")
        self.q.execute("CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(100),"
                       " emb VECTOR(4) AI COLUMN(AI_EMBED(%s,content)) WITH(max_retries=1))", (self.model,))
        self.server.slow_started.clear()
        self.server.release_slow.clear()
        with self.server.lock:
            self.server.requests.clear()

    def tearDown(self):
        self.server.release_slow.set()
        self.q.execute("ROLLBACK")
        self.q.execute("DROP TABLE IF EXISTS docs")
        self.db.close()

    def wait_vector(self, row_id, value):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            self.q.execute("SELECT emb FROM docs WHERE id=%s", (row_id,))
            row = self.q.fetchone()
            if row and row[0] is not None and json.loads(row[0]) == [value, 0, 0, 0]:
                return
            time.sleep(.1)
        self.fail("row %s did not produce vector %s" % (row_id, value))

    def index(self):
        self.q.execute("CREATE VECTOR INDEX idx_emb ON docs(emb)"
                       " WITH(distance=l2,type=hnsw,lib=vsag,m=10,ef_construction=12,ef_search=40)")

    def nearest(self, value):
        self.q.execute("SELECT id FROM docs ORDER BY l2_distance(emb,[%s,0,0,0]) APPROXIMATE LIMIT 1", (value,))
        return self.q.fetchone()

    def test_committed_regeneration_repeatedly_updates_index(self):
        self.q.execute("INSERT INTO docs(id,content) VALUES(1,'zero'),(2,'twenty')")
        self.wait_vector(1, 0)
        self.wait_vector(2, 20)
        self.index()
        self.assertEqual((1,), self.nearest(0))
        for content, value in (("ten", 10), ("twenty", 20), ("zero", 0)):
            self.q.execute("UPDATE docs SET content=%s WHERE id=1", (content,))
            self.wait_vector(1, value)
        self.assertEqual((1,), self.nearest(0))

    def test_case_only_dependency_change_invalidates_indexed_vector(self):
        self.q.execute("INSERT INTO docs(id,content) VALUES(1,'zero'),(2,'twenty')")
        self.wait_vector(1, 0)
        self.wait_vector(2, 20)
        self.index()
        self.q.execute("START TRANSACTION")
        self.q.execute("UPDATE docs SET content='ZERO' WHERE id=1")
        self.q.execute("SELECT emb FROM docs WHERE id=1")
        self.assertIsNone(self.q.fetchone()[0])
        self.assertEqual((2,), self.nearest(0))
        self.q.execute("COMMIT")
        self.wait_vector(1, 10)
        self.assertEqual((1,), self.nearest(10))
        self.q.execute("UPDATE docs SET content='ZERO ' WHERE id=1")
        self.wait_vector(1, 0)
        self.assertEqual((1,), self.nearest(0))

    def test_rollback_restores_vector_and_does_not_call_model(self):
        self.q.execute("INSERT INTO docs(id,content) VALUES(1,'zero'),(2,'twenty')")
        self.wait_vector(1, 0)
        self.wait_vector(2, 20)
        self.index()
        with self.server.lock:
            before = list(self.server.requests)
        self.q.execute("START TRANSACTION")
        self.q.execute("UPDATE docs SET content='ten' WHERE id=1")
        self.q.execute("SELECT emb FROM docs WHERE id=1")
        self.assertIsNone(self.q.fetchone()[0])
        self.assertEqual((2,), self.nearest(0))
        self.q.execute("ROLLBACK")
        self.wait_vector(1, 0)
        self.assertEqual((1,), self.nearest(0))
        time.sleep(.3)
        with self.server.lock:
            self.assertEqual(before, self.server.requests)

    def test_late_vector_cannot_overwrite_newer_update(self):
        self.q.execute("INSERT INTO docs(id,content) VALUES(1,'zero')")
        self.wait_vector(1, 0)
        self.index()
        self.q.execute("UPDATE docs SET content='slow-ten' WHERE id=1")
        self.assertTrue(self.server.slow_started.wait(10), "slow request never started")
        self.q.execute("UPDATE docs SET content='twenty' WHERE id=1")
        self.wait_vector(1, 20)
        self.server.release_slow.set()
        time.sleep(.5)
        self.wait_vector(1, 20)
        self.assertEqual((1,), self.nearest(20))

    def test_delete_during_inflight_request_does_not_restore_row(self):
        self.q.execute("INSERT INTO docs(id,content) VALUES(1,'zero')")
        self.wait_vector(1, 0)
        self.index()
        self.q.execute("UPDATE docs SET content='slow-ten' WHERE id=1")
        self.assertTrue(self.server.slow_started.wait(10), "slow request never started")
        self.q.execute("DELETE FROM docs WHERE id=1")
        self.server.release_slow.set()
        time.sleep(.5)
        self.q.execute("SELECT COUNT(*) FROM docs")
        self.assertEqual((0,), self.q.fetchone())
        self.q.execute("SELECT COUNT(*) FROM oceanbase.__all_ai_column_task task"
                       " JOIN oceanbase.__all_ai_column col ON task.table_id=col.table_id"
                       " AND task.column_id=col.column_id WHERE col.database_name=%s", (self.name,))
        self.assertEqual((0,), self.q.fetchone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
