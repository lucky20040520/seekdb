"""Cross-module committed/uncommitted visibility after forced process death."""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from minivector.engine import Database
from minivector.sql import execute


WORKER = '''
import sys
from minivector.engine import Database, DBError
from minivector.sql import execute
db=Database(sys.argv[1])
scenario=sys.argv[2]
if scenario=='partial-rewrite':
    def partial():
        yield [9,'new',[9.0,0.0]]
        print('CUT',flush=True)
        sys.stdin.readline()
        yield [10,'new',[10.0,0.0]]
    db.rewrite('t',partial())
else:
    execute(db,'BEGIN;')
    execute(db,'UPDATE t SET v=[0,0] WHERE id=2;')
    if scenario=='failed-then-commit':
        try:
            execute(db,"INSERT INTO t VALUES (3,'new',[3,0]),(2,'dup',[4,0]);")
        except DBError:
            pass
        else:
            raise AssertionError('duplicate statement unexpectedly succeeded')
    else:
        execute(db,'DELETE FROM t WHERE id=1;')
        execute(db,"INSERT INTO t VALUES (3,'new',[3,0]);")
        execute(db,'INSERT INTO u VALUES (8);')
    if scenario!='uncommitted':
        execute(db,'COMMIT;')
    print('CUT',flush=True)
    sys.stdin.readline()
'''


class CrashMatrix(unittest.TestCase):
    def kill_scenario(self, directory, scenario):
        process = subprocess.Popen([sys.executable, '-u', '-c', WORKER, directory, scenario],
                                   cwd=str(Path(__file__).resolve().parents[1]),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8',
                                   env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        events = queue.Queue()
        reader = threading.Thread(target=lambda: events.put(process.stdout.readline().strip()), daemon=True)
        reader.start()
        try:
            self.assertEqual('CUT', events.get(timeout=10))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def check_scenario(self, scenario, expected, expected_other):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(4),v VECTOR(2));')
            execute(db,'CREATE TABLE u(id BIGINT PRIMARY KEY);')
            execute(db,"INSERT INTO t VALUES (1,'old',[1,0]),(2,'old',[2,0]);")
            execute(db,'CREATE VECTOR INDEX idx ON t(v);')
            db.close()
            self.kill_scenario(directory, scenario)
            for restart in range(2):
                db = Database(directory)
                ordinary = list(execute(db,'SELECT * FROM t ORDER BY id;')[1])
                self.assertEqual(expected, ordinary)
                for mode in ('exact','ann'):
                    ranked = list(execute(db,'SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10;',mode)[1])
                    truth = sorted(expected,key=lambda row:(sum(x*x for x in row[2]),row[0]))
                    self.assertEqual([[row[0]] for row in truth], ranked)
                self.assertEqual(expected_other,list(execute(db,'SELECT id FROM u;')[1]))
                self.assertEqual({'idx'},set(db.table('t')['indexes']))
                db.close()

    def test_acknowledged_commit_survives_kill_across_tables_and_vectors(self):
        self.check_scenario('committed',[[2,'old',[0.0,0.0]],[3,'new',[3.0,0.0]]],[[8]])

    def test_uncommitted_changes_disappear_in_all_query_paths(self):
        self.check_scenario('uncommitted',[[1,'old',[1.0,0.0]],[2,'old',[2.0,0.0]]],[])

    def test_failed_statement_preserves_prior_success_on_commit_and_kill(self):
        self.check_scenario('failed-then-commit',[[1,'old',[1.0,0.0]],[2,'old',[0.0,0.0]]],[])

    def test_kill_during_row_file_write_keeps_previous_committed_snapshot(self):
        self.check_scenario('partial-rewrite',[[1,'old',[1.0,0.0]],[2,'old',[2.0,0.0]]],[])


if __name__ == '__main__':
    unittest.main()
