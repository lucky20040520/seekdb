"""Additional native AI COLUMN regressions on a dedicated local test instance.

SEEKDB_TEST_PORT is required. SEEKDB_TEST_INSTANCE optionally enables a restart
test; it must name the dedicated instance's --base-dir (never production data).
The existing competition mock is used without changing its responses.
"""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
import uuid

import pymysql


@unittest.skipUnless(os.environ.get("SEEKDB_TEST_PORT"), "requires dedicated seekdb instance")
class AiColumnIntegration(unittest.TestCase):
    @classmethod
    def connect(cls):
        return pymysql.connect(host="127.0.0.1", port=int(os.environ["SEEKDB_TEST_PORT"]),
                               user="root", autocommit=True, charset="utf8mb4")

    @classmethod
    def setUpClass(cls):
        cls.name = "ai_regression_" + uuid.uuid4().hex[:12]
        cls.model = cls.name + "_model"
        cls.endpoint = cls.name + "_ep"
        repo = Path(__file__).resolve().parents[2]
        path = repo / "tools/deploy/mysql_test/test_suite/ai_column/include/ai_column_mock.py"
        spec = importlib.util.spec_from_file_location("ai_column_mock", path)
        cls.mock = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mock)
        subprocess.run([sys.executable, str(path), "start", "19125"], check=True)
        with cls.connect() as db, db.cursor() as q:
            q.execute("CREATE DATABASE `" + cls.name + "`")
            q.execute("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(%s,%s)",
                      (cls.model, json.dumps({"type": "completion", "model_name": "mock-complete"})))
            q.execute("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(%s,%s)",
                      (cls.endpoint, json.dumps({"ai_model_name": cls.model,
                       "url": "http://127.0.0.1:19125/v1/chat/completions", "access_key": "regression-key",
                       "provider": "openai", "request_model_name": "mock-complete"})))

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.connect() as db, db.cursor() as q:
                q.execute("DROP DATABASE `" + cls.name + "`")
                q.execute("CALL DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT(%s)", (cls.endpoint,))
                q.execute("CALL DBMS_AI_SERVICE.DROP_AI_MODEL(%s)", (cls.model,))
        finally:
            cls.mock.stop_server(19125)

    def setUp(self):
        self.db = self.connect()
        self.q = self.db.cursor()
        self.q.execute("USE `" + self.name + "`")

    def tearDown(self):
        self.db.close()

    def create(self, name):
        self.q.execute("CREATE TABLE `" + name + "` (id BIGINT PRIMARY KEY, content VARCHAR(100),"
                       " result JSON AI COLUMN(AI_COMPLETE(%s,content)) WITH(max_retries=0))", (self.model,))

    def ready(self, table, count=1):
        for _ in range(150):
            self.q.execute("SELECT COUNT(*) FROM `" + table + "` WHERE result IS NOT NULL")
            if self.q.fetchone()[0] == count:
                return
            time.sleep(.1)
        self.fail("AI results did not become ready for " + table)

    def rejects(self, statement, code):
        with self.assertRaises(pymysql.MySQLError) as error:
            self.q.execute(statement)
        self.assertEqual(code, error.exception.args[0])

    def test_add_backfill_and_assignment_protection(self):
        self.q.execute("CREATE TABLE backfill(id BIGINT PRIMARY KEY,content VARCHAR(100))")
        self.q.execute("INSERT INTO backfill VALUES(1,'one'),(2,'two')")
        self.q.execute("ALTER TABLE backfill ADD result JSON AI COLUMN(AI_COMPLETE(%s,content))", (self.model,))
        self.ready("backfill", 2)
        self.rejects("UPDATE backfill SET result='{}' WHERE id=1", 1235)
        self.rejects("INSERT INTO backfill(id,content,result) VALUES(3,'three','{}')", 1235)
        self.q.execute("CREATE VIEW result_view AS SELECT id,result AS alias_result FROM backfill")
        self.rejects("UPDATE result_view SET alias_result='{}' WHERE id=1", 1235)
        self.q.execute("SELECT COUNT(*) FROM backfill")
        self.assertEqual((2,), self.q.fetchone())

    def test_strict_read_sees_own_uncommitted_invalidation(self):
        self.create("own_transaction")
        self.q.execute("INSERT INTO own_transaction(id,content) VALUES(1,'committed')")
        self.ready("own_transaction")
        self.q.execute("START TRANSACTION")
        self.q.execute("UPDATE own_transaction SET content='uncommitted-probe' WHERE id=1")
        self.q.execute("SET SESSION ai_column_strict_read=ON")
        self.rejects("SELECT result FROM own_transaction WHERE id=1", 11120)
        self.q.execute("SELECT content FROM own_transaction WHERE id=1")
        self.assertEqual(("uncommitted-probe",), self.q.fetchone())
        self.q.execute("ROLLBACK")
        self.q.execute("SELECT result FROM own_transaction")
        self.assertEqual("committed", json.loads(self.q.fetchone()[0])["input"])
        time.sleep(.2)
        self.assertFalse(any("uncommitted-probe" in x["user"] for x in self.mock.read_events(19125)))

    def test_prepared_strict_read_checks_each_execution(self):
        self.create("prepared_read")
        self.q.execute("INSERT INTO prepared_read(id,content) VALUES(1,'ready')")
        self.ready("prepared_read")
        self.q.execute("SET SESSION ai_column_strict_read=ON")
        self.q.execute("PREPARE ai_result_read FROM 'SELECT result FROM prepared_read WHERE id=?'")
        try:
            self.q.execute("SET @ai_row_id=1")
            self.q.execute("EXECUTE ai_result_read USING @ai_row_id")
            self.assertIsNotNone(self.q.fetchone()[0])
            self.q.execute("START TRANSACTION")
            self.q.execute("UPDATE prepared_read SET content='not-committed' WHERE id=1")
            self.rejects("EXECUTE ai_result_read USING @ai_row_id", 11120)
            self.q.execute("ROLLBACK")
            self.q.execute("EXECUTE ai_result_read USING @ai_row_id")
            self.assertIsNotNone(self.q.fetchone()[0])
        finally:
            self.q.execute("ROLLBACK")
            self.q.execute("DEALLOCATE PREPARE ai_result_read")

    def test_null_input_is_ready_null_without_inference(self):
        self.create("null_input")
        before = len(self.mock.read_events(19125))
        self.q.execute("INSERT INTO null_input(id,content) VALUES(1,NULL)")
        for _ in range(100):
            self.q.execute("SELECT COUNT(*) FROM oceanbase.__all_ai_column_task task"
                           " JOIN oceanbase.__all_ai_column col ON task.table_id=col.table_id"
                           " AND task.column_id=col.column_id WHERE col.database_name=%s"
                           " AND col.table_name='null_input'", (self.name,))
            if self.q.fetchone()[0] == 0:
                break
            time.sleep(.1)
        else:
            self.fail("NULL input task did not complete")
        self.q.execute("SET SESSION ai_column_strict_read=ON")
        self.q.execute("SELECT result FROM null_input")
        self.assertEqual((None,), self.q.fetchone())
        self.assertEqual(before, len(self.mock.read_events(19125)))

    def test_new_task_is_eligible_in_own_transaction(self):
        self.create("immediate_task")
        for i in range(50):
            self.q.execute("START TRANSACTION")
            self.q.execute("INSERT INTO immediate_task(id,content) VALUES(%s,'eligible')", (i,))
            self.q.execute("SELECT COUNT(*), MAX(task.eligible_ts <= NOW(6)),"
                           " MAX(task.eligible_ts), NOW(6) FROM oceanbase.__all_ai_column_task task"
                           " JOIN oceanbase.__all_table t ON t.table_id=task.table_id"
                           " JOIN oceanbase.__all_database d ON d.database_id=t.database_id"
                           " WHERE d.database_name=%s AND t.table_name='immediate_task'", (self.name,))
            row = self.q.fetchone()
            self.assertEqual((1, 1), row[:2], repr(row))
            self.q.execute("ROLLBACK")

    def test_table_name_reuse_does_not_reuse_column_ids(self):
        for _ in range(4):
            self.create("recreated")
            self.q.execute("SHOW AI COLUMNS FROM recreated")
            self.assertEqual(1, len(self.q.fetchall()))
            self.q.execute("DROP TABLE recreated")

    @unittest.skipUnless(os.environ.get("SEEKDB_TEST_INSTANCE"), "restart requires dedicated instance base-dir")
    def test_restart_recovers_persisted_claim_after_lease_expiry(self):
        self.create("recovery")
        self.q.execute("START TRANSACTION")
        self.q.execute("INSERT INTO recovery(id,content) VALUES(1,'recover-after-restart')")
        self.q.execute("UPDATE oceanbase.__all_ai_column_task task JOIN oceanbase.__all_ai_column col"
                       " ON task.table_id=col.table_id AND task.column_id=col.column_id"
                       " SET task.owner='interrupted-regression',task.lease_until=DATE_ADD(NOW(6),INTERVAL 1 HOUR)"
                       " WHERE col.database_name=%s AND col.table_name='recovery'", (self.name,))
        self.q.execute("COMMIT")
        instance = str(Path(os.environ["SEEKDB_TEST_INSTANCE"]).resolve())
        matches = []
        for path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                args = path.read_bytes().rstrip(b"\0").decode().split("\0")
                if "--base-dir" in args and args[args.index("--base-dir") + 1] == instance:
                    matches.append((int(path.parent.name), args))
            except (OSError, UnicodeDecodeError):
                continue
        self.assertEqual(1, len(matches), "must identify exactly one dedicated instance")
        pid, args = matches[0]
        self.db.close()
        os.kill(pid, signal.SIGTERM)
        for _ in range(300):
            if not Path("/proc", str(pid)).exists():
                break
            time.sleep(.1)
        else:
            self.fail("dedicated instance did not stop")
        with open("/tmp/seekdb-ai-column-regression-restart.log", "a") as output:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        pid_file = Path(os.environ.get("SEEKDB_TEST_PID_FILE", "/tmp/seekdb-ai-column-instance.pid"))
        pid_file.write_text(str(process.pid))
        for _ in range(300):
            try:
                self.db = self.connect()
                break
            except pymysql.MySQLError:
                self.assertIsNone(process.poll(), "dedicated instance exited")
                time.sleep(.1)
        else:
            self.fail("dedicated instance did not restart")
        self.q = self.db.cursor()
        self.q.execute("USE `" + self.name + "`")
        self.q.execute("SELECT COUNT(*) FROM oceanbase.__all_ai_column_task task"
                       " JOIN oceanbase.__all_ai_column col ON task.table_id=col.table_id"
                       " AND task.column_id=col.column_id WHERE col.database_name=%s"
                       " AND col.table_name='recovery' AND task.owner='interrupted-regression'", (self.name,))
        self.assertEqual((1,), self.q.fetchone())
        self.q.execute("UPDATE oceanbase.__all_ai_column_task SET lease_until=DATE_SUB(NOW(6),INTERVAL 1 SECOND)"
                       " WHERE owner='interrupted-regression'")
        self.ready("recovery")
        self.q.execute("SELECT result FROM recovery")
        self.assertEqual("recover-after-restart", json.loads(self.q.fetchone()[0])["input"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
