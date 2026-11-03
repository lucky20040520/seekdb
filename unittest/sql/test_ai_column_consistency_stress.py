"""Reproduce AI-column task eligibility and concurrent DDL failures.

Run only against a dedicated local test instance with SEEKDB_TEST_PORT set.
All databases, model names, and tables have a random per-run prefix.
"""

import json
import os
import threading
import unittest
import uuid

import pymysql


@unittest.skipUnless(os.environ.get("SEEKDB_TEST_PORT"), "requires a dedicated seekdb instance")
class AiColumnConsistencyStress(unittest.TestCase):
    @classmethod
    def connect(cls):
        return pymysql.connect(host="127.0.0.1", port=int(os.environ["SEEKDB_TEST_PORT"]),
                               user="root", autocommit=True, charset="utf8mb4", read_timeout=60)

    @classmethod
    def setUpClass(cls):
        cls.name = "ai_consistency_" + uuid.uuid4().hex[:10]
        cls.model = cls.name + "_model"
        cls.endpoint = cls.name + "_endpoint"
        cls.db_created = cls.model_created = cls.endpoint_created = False
        cls.addClassCleanup(cls.cleanup)
        with cls.connect() as db, db.cursor() as q:
            q.execute("CREATE DATABASE `" + cls.name + "`")
            cls.db_created = True
            q.execute("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(%s,%s)",
                      (cls.model, json.dumps({"type": "completion", "model_name": "test"})))
            cls.model_created = True
            q.execute("CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(%s,%s)",
                      (cls.endpoint, json.dumps({"ai_model_name": cls.model,
                       "url": "http://127.0.0.1:1/v1/chat/completions", "access_key": "test-key",
                       "provider": "openai", "request_model_name": "test"})))
            cls.endpoint_created = True

    @classmethod
    def cleanup(cls):
        with cls.connect() as db, db.cursor() as q:
            if cls.db_created:
                q.execute("DROP DATABASE `" + cls.name + "`")
            if cls.endpoint_created:
                q.execute("CALL DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT(%s)", (cls.endpoint,))
            if cls.model_created:
                q.execute("CALL DBMS_AI_SERVICE.DROP_AI_MODEL(%s)", (cls.model,))

    def setUp(self):
        self.db = self.connect()
        self.q = self.db.cursor()
        self.q.execute("USE `" + self.name + "`")

    def tearDown(self):
        self.db.close()

    def test_concurrent_ai_and_ordinary_ddl(self):
        iterations = int(os.environ.get("AI_DDL_ITERATIONS", "20"))
        start = threading.Barrier(2, timeout=20)
        errors = []
        results = []
        lock = threading.Lock()

        def run(ai):
            db = self.connect()
            q = db.cursor()
            try:
                q.execute("USE `" + self.name + "`")
                start.wait()
                for i in range(iterations):
                    table = ("ai_" if ai else "ordinary_") + str(i)
                    try:
                        if ai:
                            q.execute("CREATE TABLE `" + table + "` (id BIGINT PRIMARY KEY,"
                                      " content VARCHAR(100), result VARCHAR(200)"
                                      " AI COLUMN(AI_COMPLETE(%s,content)))", (self.model,))
                            q.execute("SELECT COUNT(*) FROM oceanbase.__all_ai_column "
                                      "WHERE database_name=%s AND table_name=%s", (self.name, table))
                            if q.fetchone()[0] != 1:
                                raise AssertionError("successful CREATE did not install metadata: " + table)
                        else:
                            q.execute("CREATE TABLE `" + table + "` (id BIGINT PRIMARY KEY, content VARCHAR(100))")
                        q.execute("DROP TABLE `" + table + "`")
                        with lock:
                            results.append((ai, i))
                    except (pymysql.MySQLError, AssertionError) as error:
                        with lock:
                            errors.append(("ai" if ai else "ordinary", i, error.args))
            except Exception as error:
                with lock:
                    errors.append(("worker", ai, repr(error)))
            finally:
                db.close()

        threads = [threading.Thread(target=run, args=(True,)), threading.Thread(target=run, args=(False,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "DDL workers timed out")
        print("concurrent_ddl_iterations=%d successful=%d errors=%r" % (iterations, len(results), errors), flush=True)
        self.assertEqual([], errors)
        self.assertEqual(iterations * 2, len(results))

    def test_failed_add_preserves_existing_hidden_named_column(self):
        self.q.execute("CREATE TABLE collision (id BIGINT PRIMARY KEY, content VARCHAR(100),"
                       " `__ai_state_result` VARCHAR(20))")
        self.q.execute("INSERT INTO collision VALUES (1,'input','keep')")
        with self.assertRaises(pymysql.MySQLError):
            self.q.execute("ALTER TABLE collision ADD COLUMN result VARCHAR(200)"
                           " AI COLUMN(AI_COMPLETE(%s,content))", (self.model,))
        self.q.execute("SELECT content,`__ai_state_result` FROM collision WHERE id=1")
        self.assertEqual(("input", "keep"), self.q.fetchone())
        self.q.execute("SHOW COLUMNS FROM collision LIKE 'result'")
        self.assertIsNone(self.q.fetchone())
        self.q.execute("SELECT COUNT(*) FROM oceanbase.__all_ai_column "
                       "WHERE database_name=%s AND table_name='collision'", (self.name,))
        self.assertEqual(0, self.q.fetchone()[0])

    def test_create_if_not_exists_keeps_existing_table(self):
        self.q.execute("CREATE TABLE already_exists (id BIGINT PRIMARY KEY, content VARCHAR(100))")
        self.q.execute("INSERT INTO already_exists VALUES (1,'keep')")
        self.q.execute("CREATE TABLE IF NOT EXISTS already_exists (id BIGINT PRIMARY KEY,"
                       " content VARCHAR(100), result VARCHAR(200)"
                       " AI COLUMN(AI_COMPLETE(%s,content)))", (self.model,))
        self.q.execute("SELECT content FROM already_exists WHERE id=1")
        self.assertEqual(("keep",), self.q.fetchone())
        self.q.execute("SHOW COLUMNS FROM already_exists LIKE 'result'")
        self.assertIsNone(self.q.fetchone())

    def test_new_task_is_eligible_in_same_transaction(self):
        iterations = int(os.environ.get("AI_TASK_ITERATIONS", "200"))
        self.q.execute("CREATE TABLE task_rows (id BIGINT PRIMARY KEY, content VARCHAR(100),"
                       " result VARCHAR(200) AI COLUMN(AI_COMPLETE(%s,content)))", (self.model,))
        for i in range(iterations):
            self.q.execute("START TRANSACTION")
            try:
                self.q.execute("INSERT INTO task_rows(id,content) VALUES(%s,'probe')", (i,))
                self.q.execute("SELECT COUNT(*),MAX(task.eligible_ts <= NOW(6)),"
                               "MIN(task.eligible_ts),MAX(task.eligible_ts),NOW(6),"
                               "TIMESTAMPDIFF(MICROSECOND,MAX(task.eligible_ts),NOW(6))"
                               " FROM oceanbase.__all_ai_column_task task"
                               " JOIN oceanbase.__all_table t ON t.table_id=task.table_id"
                               " JOIN oceanbase.__all_database d ON d.database_id=t.database_id"
                               " WHERE d.database_name=%s AND t.table_name='task_rows'", (self.name,))
                row = self.q.fetchone()
                self.assertEqual((1, 1), row[:2], "iteration=%d details=%r" % (i, row))
            finally:
                self.q.execute("ROLLBACK")
        print("task_eligibility_iterations=%d all_passed" % iterations, flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
