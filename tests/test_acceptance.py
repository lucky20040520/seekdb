import json
import math
import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest
import tracemalloc
from pathlib import Path

from minivector.engine import Database, DBError
from minivector.sql import execute


ROOT = Path(__file__).resolve().parents[1]


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name)
        execute(self.db, "CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(8), v VECTOR(2));")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def rows(self, sql):
        headers, rows = execute(self.db, sql)
        return list(rows)

    def test_crud_filter_and_projection(self):
        execute(self.db, "INSERT INTO t(v,id,tag) VALUES ([3,4],2,'乙'),([0,0],1,'甲');")
        self.assertEqual([[1, '甲'], [2, '乙']], self.rows("SELECT id, tag FROM t WHERE id >= 1 AND id < 3 ORDER BY id;"))
        self.assertEqual([[2]], self.rows("SELECT id FROM t WHERE tag < '甲' AND id <= 2;"))
        self.assertEqual(1, execute(self.db, "UPDATE t SET tag='新',v=[1,2] WHERE id=2;"))
        self.assertEqual([[2, '新', [1.0, 2.0]]], self.rows("SELECT * FROM t WHERE id=2;"))
        self.assertEqual(1, execute(self.db, "DELETE FROM t WHERE id>1;"))
        self.assertEqual([[1]], self.rows("SELECT id FROM t;"))

    def test_statement_atomicity_inside_transaction(self):
        execute(self.db, "BEGIN;")
        execute(self.db, "INSERT INTO t VALUES (1,'ok',[0,0]);")
        for sql in ["INSERT INTO t VALUES (2,'x',[0,0]),(1,'dup',[2,3]);",
                    "INSERT INTO t VALUES (2,'x',[0,0]),(3,'too-long-value',[2,3]);",
                    "UPDATE t SET tag='too-long-value';"]:
            with self.assertRaises(DBError): execute(self.db, sql)
            self.assertEqual([[1, 'ok', [0.0, 0.0]]], self.rows("SELECT * FROM t;"))
        execute(self.db, "COMMIT;")
        self.db.close()
        self.db = Database(self.temp.name)
        self.assertEqual([[1]], self.rows("SELECT id FROM t;"))

    def test_type_boundaries(self):
        for sql in ["INSERT INTO t VALUES (9223372036854775808,'a',[0,0]);",
                    "INSERT INTO t VALUES (-9223372036854775809,'a',[0,0]);",
                    "INSERT INTO t VALUES (1.0,'a',[0,0]);",
                    "INSERT INTO t VALUES (1,'a',[0]);",
                    "INSERT INTO t VALUES (1,'a',[NaN,0]);",
                    "INSERT INTO t VALUES (1,'a',[Infinity,0]);",
                    "INSERT INTO t VALUES (1,'a',[1e100,0]);",
                    "INSERT INTO t VALUES (1,'a',['x',0]);"]:
            with self.assertRaises(DBError): execute(self.db, sql)
        execute(self.db, "INSERT INTO t VALUES (-9223372036854775808,'汉😀字',[0,0]),(9223372036854775807,'é',[1,1]);")
        self.assertEqual(2, len(self.rows("SELECT * FROM t;")))

    def test_schema_and_rejection(self):
        for sql in ["CREATE TABLE bad(id BIGINT);", "CREATE TABLE bad(id BIGINT PRIMARY KEY,v VECTOR(0));",
                    "CREATE TABLE bad(id BIGINT PRIMARY KEY,v VECTOR(2),w VECTOR(2));",
                    "CREATE TABLE bad(id BIGINT PRIMARY KEY,id VARCHAR(2));", "CREATE TABLE bad(x FLOAT PRIMARY KEY);",
                    "SELECT missing FROM t;", "SELECT * FROM t WHERE missing=1;",
                    "SELECT * FROM t LIMIT -1;", "SELECT * FROM t LIMIT 1.5;",
                    "SELECT * FROM t JOIN t;", "UPDATE t SET id=3;", "DELETE FROM t WHERE id != 1;",
                    "SELECT * FROM t; DELETE FROM t;", "SELECT COUNT(*) FROM t;"]:
            with self.assertRaises(DBError, msg=sql):
                result = execute(self.db, sql)
                if isinstance(result, tuple): list(result[1])
        execute(self.db, "CREATE TABLE b(x BIGINT,PRIMARY KEY(x));")

    def test_filtered_distance_and_ties(self):
        execute(self.db, "INSERT INTO t VALUES (3,'yes',[3,4]),(2,'no',[0,0]),(1,'yes',[-3,-4]);")
        self.assertEqual([[1, 5.0], [3, 5.0]], self.rows("SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t WHERE tag='yes' ORDER BY d LIMIT 10;"))
        self.assertEqual([[1]], self.rows("SELECT id FROM t WHERE tag='yes' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1;"))
        self.assertEqual([], self.rows("SELECT id FROM t WHERE tag='absent' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;"))
        self.assertEqual([], self.rows("SELECT * FROM t LIMIT 0;"))

    def test_float32_storage_distance(self):
        execute(self.db, "INSERT INTO t VALUES (1,'a',[16777217,0]),(2,'a',[16777216,0]);")
        self.assertEqual([[1, 0.0], [2, 0.0]], self.rows("SELECT id,L2_DISTANCE(v,[16777216,0]) AS d FROM t ORDER BY d LIMIT 2;"))

    def test_transactions_vector_index_restart(self):
        execute(self.db, "INSERT INTO t VALUES (1,'a',[1,0]),(2,'a',[2,0]);")
        execute(self.db, "CREATE VECTOR INDEX idx ON t(v);")
        execute(self.db, "BEGIN;")
        execute(self.db, "UPDATE t SET v=[0,0] WHERE id=2;")
        execute(self.db, "DELETE FROM t WHERE id=1;")
        execute(self.db, "INSERT INTO t VALUES (3,'a',[3,0]);")
        self.assertEqual([[2], [3]], self.rows("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;"))
        execute(self.db, "ROLLBACK;")
        self.assertEqual([[1], [2]], self.rows("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;"))
        execute(self.db, "BEGIN;")
        execute(self.db, "UPDATE t SET v=[0,0] WHERE id=2;")
        execute(self.db, "COMMIT;")
        self.db.close()
        self.db = Database(self.temp.name)
        self.assertEqual([[2], [1]], self.rows("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;"))
        headers, result = execute(self.db, "SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;", mode="ann")
        self.assertEqual([[2], [1]], list(result))

    def test_unicode_order_and_escaping(self):
        execute(self.db, "INSERT INTO t VALUES (3,'中',[0,0]),(2,'a'';b',[0,0]),(1,'😀',[0,0]),(4,'中',[0,0]);")
        self.assertEqual([[1], [3], [4], [2]], self.rows("SELECT id FROM t ORDER BY tag DESC;"))
        self.assertEqual([[2]], self.rows("SELECT id FROM t WHERE tag='a'';b';"))

    def test_transaction_errors_and_close(self):
        with self.assertRaises(DBError): execute(self.db, "COMMIT;")
        with self.assertRaises(DBError): execute(self.db, "ROLLBACK;")
        execute(self.db, "BEGIN;")
        with self.assertRaises(DBError): execute(self.db, "BEGIN;")
        execute(self.db, "INSERT INTO t VALUES (1,'a',[0,0]);")
        self.db.close()
        self.db = Database(self.temp.name)
        self.assertEqual([], self.rows("SELECT * FROM t;"))

    def test_randomized_reference(self):
        rng = random.Random(771)
        reference, before = {}, None
        for step in range(180):
            action = rng.choice(["insert", "update", "delete", "begin", "commit", "rollback", "query"])
            key = rng.randrange(16)
            if action == "begin" and before is None:
                before = dict(reference)
                execute(self.db, "BEGIN;")
            elif action in ("commit", "rollback") and before is not None:
                execute(self.db, action.upper() + ";")
                if action == "rollback": reference = before
                before = None
            elif action == "insert" and key not in reference:
                vector = [rng.uniform(-8, 8), rng.uniform(-8, 8)]
                stored = [struct.unpack('<f', struct.pack('<f', v))[0] for v in vector]
                execute(self.db, "INSERT INTO t VALUES (%d,'g%d',[%r,%r]);" % (key, key % 3, *vector))
                reference[key] = ('g%d' % (key % 3), stored)
            elif action == "update":
                execute(self.db, "UPDATE t SET v=[3,4] WHERE id=%d;" % key)
                if key in reference: reference[key] = (reference[key][0], [3.0, 4.0])
            elif action == "delete":
                execute(self.db, "DELETE FROM t WHERE id=%d;" % key)
                reference.pop(key, None)
            group = 'g%d' % rng.randrange(3)
            expected = sorted(((math.sqrt(sum(v*v for v in vec)), k) for k, (tag, vec) in reference.items() if tag == group))[:5]
            actual = self.rows("SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t WHERE tag='%s' ORDER BY d LIMIT 5;" % group)
            self.assertEqual([k for d,k in expected], [r[0] for r in actual], "step %d" % step)
            for (distance, key), row in zip(expected, actual): self.assertAlmostEqual(distance, row[1])

    def test_external_sort_streaming(self):
        # Force multiple disk runs without using the SQL parser's whole-input buffer.
        def source():
            for i in range(9000, -1, -1): yield [i, 'x' * 1000]
        output = self.db.external_sort(source(), key=lambda row: row[0])
        self.assertEqual(list(range(9001)), [row[0] for row in output])
        self.assertEqual([], list(Path(self.temp.name).glob('sort-*')))

    def child(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.Popen([sys.executable, "-u", "-m", "minivector", "--data-dir", self.temp.name],
                                cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)

    def test_kill_after_ack_and_uncommitted(self):
        child = self.child()
        try:
            self.assertEqual('LOCAL_READY', child.stdout.readline().strip())
            for statement in ["INSERT INTO t VALUES (1,'a',[1,0]);", "BEGIN;", "INSERT INTO t VALUES (2,'b',[0,0]);"]:
                child.stdin.write(statement + '\n'); child.stdin.flush()
                self.assertTrue(child.stdout.readline().startswith('OK'))
            child.kill(); child.wait(timeout=10)
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr): stream.close()
        self.db = Database(self.temp.name)
        self.assertEqual([[1]], self.rows("SELECT id FROM t;"))

    def test_local_multistatement_adapter(self):
        result = subprocess.run([sys.executable, '-m', 'minivector', '--data-dir', self.temp.name], cwd=str(ROOT),
            input="INSERT INTO t VALUES (1,'a;''b',[0,0]); /* ; */ SELECT id FROM t;\n", text=True,
            encoding='utf-8', capture_output=True, timeout=15, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['LOCAL_READY', 'OK 1', 'id', '1', 'END'], result.stdout.splitlines())

    def test_large_limit_uses_disk_sort(self):
        execute(self.db, "INSERT INTO t VALUES (3,'a',[0,0]),(1,'b',[0,0]),(2,'a',[0,0]);")
        self.assertEqual([[1],[2],[3]], self.rows("SELECT id FROM t ORDER BY id LIMIT 999999999;"))

    def test_storage_streaming_memory(self):
        execute(self.db, "CREATE TABLE big(id BIGINT PRIMARY KEY, payload VARCHAR(1024));")
        tracemalloc.start()
        try:
            self.db.rewrite('big', ([i, 'x'*1024] for i in range(12000)))
            total = sum(1 for row in self.db.rows(self.db.table('big')))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(12000, total)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_commit_interruption_two_tables(self):
        execute(self.db, "CREATE TABLE b(id BIGINT PRIMARY KEY);")
        for stage in ('before', 'after'):
            script = '''
import sys
import minivector.engine as engine
from minivector.sql import execute
db=engine.Database(sys.argv[1])
execute(db,'BEGIN;')
execute(db,"INSERT INTO t VALUES (5,'a',[0,0]);")
execute(db,'INSERT INTO b VALUES (5);')
original=engine.os.replace
def interrupted(*args):
    if sys.argv[2]=='after': original(*args)
    print('COMMIT_BOUNDARY',flush=True)
    sys.stdin.readline()
    if sys.argv[2]=='before': original(*args)
engine.os.replace=interrupted
execute(db,'COMMIT;')
'''
            child = subprocess.Popen([sys.executable, '-u', '-c', script, self.temp.name, stage], cwd=str(ROOT),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                self.assertEqual('COMMIT_BOUNDARY', child.stdout.readline().strip())
                child.kill(); child.wait(timeout=10)
            finally:
                if child.poll() is None: child.kill(); child.wait(timeout=10)
                for stream in (child.stdin, child.stdout, child.stderr): stream.close()
            self.db = Database(self.temp.name)
            expected = [] if stage == 'before' else [[5]]
            self.assertEqual(expected, self.rows('SELECT id FROM t;'))
            self.assertEqual(expected, self.rows('SELECT id FROM b;'))

    def test_recovery_interrupted_during_cleanup(self):
        execute(self.db, "INSERT INTO t VALUES (1,'ok',[1,0]);")
        for i in range(4):
            (Path(self.temp.name)/('rows-orphan%d.jsonl'%i)).write_text('incomplete',encoding='utf-8')
        script = '''
import sys
from pathlib import Path
from minivector.engine import Database
original=Path.unlink
def interrupted(self,*args,**kwargs):
    original(self,*args,**kwargs)
    print('RECOVERY_BOUNDARY',flush=True)
    sys.stdin.readline()
Path.unlink=interrupted
Database(sys.argv[1])
'''
        child = subprocess.Popen([sys.executable,'-u','-c',script,self.temp.name],cwd=str(ROOT),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        try:
            self.assertEqual('RECOVERY_BOUNDARY',child.stdout.readline().strip())
            child.kill(); child.wait(timeout=10)
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=10)
            for stream in (child.stdin,child.stdout,child.stderr): stream.close()
        self.db=Database(self.temp.name)
        self.assertEqual([[1]],self.rows('SELECT id FROM t;'))
        self.assertEqual([],list(Path(self.temp.name).glob('rows-orphan*')))


if __name__ == '__main__': unittest.main()
