import json
import os
import queue
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from minivector.engine import Database
from minivector.sql import execute
from minivector import flat_index


def results(db,sql,mode='exact'):
    return list(execute(db,sql,mode=mode)[1])


class FlatIndexContracts(unittest.TestCase):
    def run_interrupted_worker(self,directory,script):
        process=subprocess.Popen([sys.executable,'-u','-c',script,directory],
            cwd=str(Path(__file__).resolve().parents[1]),stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',
            env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        output,errors=queue.Queue(),queue.Queue()
        def collect(stream,target):
            for line in stream:target.put(line.strip())
        threads=[threading.Thread(target=collect,args=(process.stdout,output),daemon=True),
                 threading.Thread(target=collect,args=(process.stderr,errors),daemon=True)]
        for thread in threads:thread.start()
        try:
            self.assertEqual('INDEX_WRITTEN',errors.get(timeout=10))
            with self.assertRaises(queue.Empty):output.get(timeout=0.1)
        finally:
            if process.poll() is None:process.kill()
            process.wait(timeout=10)
            for thread in threads:thread.join(timeout=2)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def setup_table(self,directory):
        db=Database(directory)
        execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(2),v VECTOR(2));')
        execute(db,"INSERT INTO t VALUES (3,'b','[0,2]'),(1,'a','[0,0]'),(2,'a','[1,0]');")
        execute(db,'CREATE VECTOR INDEX idx ON t(v);')
        return db

    def test_actual_files_and_index_query_path(self):
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory)
            descriptor=db.table('t')['indexes']['idx']
            self.assertEqual(flat_index.ALGORITHM,descriptor['algorithm'])
            self.assertEqual(set(db.files(db.table('t'))),set(descriptor['segments']))
            for row_file,index_file in descriptor['segments'].items():
                self.assertTrue(flat_index.valid(db.path,row_file,index_file,db.table('t'),'v'))
                self.assertGreater((db.path/index_file).stat().st_size,flat_index.HEADER.size)
            # Prove this is the physical index path, not the generic row scan or cache.
            with patch.object(db,'rows',side_effect=AssertionError('generic scan used')), \
                 patch.object(db,'cached_vectors',side_effect=AssertionError('cache used')):
                self.assertEqual([[1],[2],[3]],results(db,"SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;"))
                self.assertEqual([[3]],results(db,"SELECT id FROM t WHERE tag='b' ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 2;",'ann'))
            db.close()

    def test_updates_failures_rollback_commit_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory)
            old=dict(db.table('t')['indexes']['idx']['segments'])
            execute(db,'BEGIN;')
            execute(db,"UPDATE t SET v='[0,0]' WHERE id=3;")
            execute(db,'DELETE FROM t WHERE id=1;')
            execute(db,"INSERT INTO t VALUES (4,'a','[0,0]');")
            query="SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;"
            self.assertEqual([[3],[4],[2]],results(db,query,'ann'))
            execute(db,'ROLLBACK;')
            self.assertEqual(old,db.table('t')['indexes']['idx']['segments'])
            self.assertEqual([[1],[2],[3]],results(db,query))
            referenced=set(old.values())
            self.assertEqual(referenced,{p.name for p in db.path.glob('index-*.flat')})
            execute(db,'BEGIN;')
            execute(db,"UPDATE t SET v='[0,0]' WHERE id=3;")
            execute(db,'COMMIT;')
            db.close();db=Database(directory)
            self.assertEqual([[1],[3],[2]],results(db,query))
            self.assertEqual([[1],[3],[2]],results(db,query,'ann'))
            db.close()

    def test_index_build_failure_does_not_publish_a_partial_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory)
            old=json.loads(json.dumps(db.catalog))
            with patch.object(flat_index,'build',side_effect=OSError('index build interrupted')):
                with self.assertRaises(OSError):
                    execute(db,"UPDATE t SET v='[9,9]' WHERE id=1;")
            self.assertEqual(old,db.catalog)
            expected_files=set(db.files(db.table('t')))
            self.assertEqual(expected_files,{p.name for p in db.path.glob('rows-*.jsonl')})
            self.assertEqual([[1],[2],[3]],results(db,"SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;"))
            db.close()

    def test_missing_and_legacy_indexes_rebuild_on_open(self):
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory)
            query="SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;"
            descriptor=db.catalog['t']['indexes']['idx']
            (db.path/next(iter(descriptor['segments'].values()))).unlink()
            db=Database(directory)
            self.assertEqual([[1],[2],[3]],results(db,query))
            legacy=json.loads(json.dumps(db.catalog))
            legacy['t']['indexes']['idx']={'column':'v','algorithm':'exact_scan'}
            db.publish(legacy);db.close()
            db=Database(directory)
            self.assertEqual(flat_index.ALGORITHM,db.catalog['t']['indexes']['idx']['algorithm'])
            self.assertEqual([[1],[2],[3]],results(db,query,'ann'))
            db.close()

    def test_index_created_inside_rolled_back_transaction_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(directory)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db,"INSERT INTO t VALUES (1,'[0,0]');")
            execute(db,'BEGIN;')
            execute(db,'CREATE VECTOR INDEX idx ON t(v);')
            self.assertTrue(list(db.path.glob('index-*.flat')))
            execute(db,'ROLLBACK;')
            self.assertEqual({},db.table('t')['indexes'])
            self.assertEqual([],list(db.path.glob('index-*.flat')))
            db.close()

    def test_randomized_index_scan_and_reference_states_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory)
            # A second table with the same schema but no index is an independent
            # execution path; deterministic mutations are applied to both.
            execute(db,'CREATE TABLE plain(id BIGINT PRIMARY KEY,tag VARCHAR(2),v VECTOR(2));')
            execute(db,"INSERT INTO plain VALUES (3,'b','[0,2]'),(1,'a','[0,0]'),(2,'a','[1,0]');")
            rng=random.Random(991)
            for step in range(40):
                primary=step+4
                vector=[rng.randrange(-5,6),rng.randrange(-5,6)]
                execute(db,'BEGIN;')
                for name in ('t','plain'):
                    execute(db,"INSERT INTO %s VALUES (%d,'a','%s');"%(name,primary,json.dumps(vector)))
                    execute(db,"UPDATE %s SET v='[2,3]' WHERE id=%d;"%(name,primary-1))
                    if step%3==0:execute(db,'DELETE FROM %s WHERE id=%d;'%(name,primary-2))
                for where in ('',"WHERE tag='b'",'WHERE id>=8'):
                    for order in ('ASC','DESC'):
                        template="SELECT id,L2_DISTANCE(v,'[1,1]') AS d FROM %s "+where+' ORDER BY d '+order+' LIMIT 10;'
                        self.assertEqual(results(db,template%'plain'),results(db,template%'t','ann'))
                execute(db,'ROLLBACK;' if step%4==0 else 'COMMIT;')
            db.close()

    def test_rebuild_can_be_killed_twice_before_readiness(self):
        script='''
import runpy,sys
from minivector import flat_index
original=flat_index.build
def paused(*args):
    filename=original(*args)
    print('INDEX_WRITTEN',file=sys.stderr,flush=True)
    sys.stdin.readline()
    return filename
flat_index.build=paused
sys.argv=['minivector','--data-dir',sys.argv[1]]
runpy.run_module('minivector',run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory)
            index=next(iter(db.table('t')['indexes']['idx']['segments'].values()))
            (db.path/index).unlink()
            db.close()
            self.run_interrupted_worker(directory,script)
            self.run_interrupted_worker(directory,script)
            # A normal CLI can report ready only after rebuilding the index.
            result=subprocess.run([sys.executable,'-m','minivector','--data-dir',directory, '--protocol', 'diagnostic'],
                cwd=str(Path(__file__).resolve().parents[1]),
                input="SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;\n",
                text=True,encoding='utf-8',capture_output=True,timeout=15,
                env=dict(os.environ,PYTHONIOENCODING='utf-8'))
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual(['LOCAL_READY','id','1','2','3','END'],result.stdout.splitlines())

    def test_kill_during_index_maintenance_keeps_committed_generation(self):
        script='''
import sys
from minivector.engine import Database
from minivector.sql import execute
from minivector import flat_index
db=Database(sys.argv[1])
original=flat_index.build
def paused(*args):
    filename=original(*args)
    print('INDEX_WRITTEN',file=sys.stderr,flush=True)
    sys.stdin.readline()
    return filename
flat_index.build=paused
execute(db,"UPDATE t SET v='[9,9]';")
'''
        with tempfile.TemporaryDirectory() as directory:
            db=self.setup_table(directory);db.close()
            self.run_interrupted_worker(directory,script)
            db=Database(directory)
            self.assertEqual([[1],[2],[3]],results(db,"SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;"))
            self.assertEqual([[1,[0.0,0.0]],[2,[1.0,0.0]],[3,[0.0,2.0]]],results(db,'SELECT id,v FROM t ORDER BY id;'))
            live=set(db.table('t')['indexes']['idx']['segments'].values())
            self.assertEqual(live,{p.name for p in db.path.glob('index-*.flat')})
            db.close()


if __name__=='__main__':unittest.main()
