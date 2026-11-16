"""Journal durability boundaries, bounded storage, migration and checkpoint recovery."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from minivector import engine, journal, segments
from minivector.engine import Database
from minivector.sql import execute


def rows(db, name='t'):
    return list(execute(db, 'SELECT * FROM '+name+' ORDER BY id;')[1])


class JournalContracts(unittest.TestCase):
    def test_large_rewrite_external_segment_has_same_atomicity_and_index_contract(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db, 'INSERT INTO t VALUES(1,[1,0]),(2,[2,0]);')
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            with patch.object(engine, 'JOURNAL_REWRITE_LIMIT', 1):
                execute(db, 'BEGIN;')
                execute(db, 'UPDATE t SET v=[0,0] WHERE id=2;')
                filename = db.files(db.table('t'))[0]
                self.assertIsNone(journal.reference(filename))
                self.assertTrue((Path(root)/filename).exists())
                db.query_cache_limit = 0
                query = 'SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1;'
                self.assertEqual([[2]], list(execute(db, query)[1]))
                execute(db, 'ROLLBACK;')
                self.assertFalse((Path(root)/filename).exists())
                self.assertEqual([[1]], list(execute(db, query)[1]))
                def broken():
                    yield [9, [0.0, 0.0]]
                    raise OSError('failed external stream')
                with self.assertRaises(OSError):
                    db.rewrite('t', broken())
                self.assertEqual([], list(Path(root).glob('rows-*.mvb')))
                with patch.object(db, 'prepare_indexes', side_effect=OSError('failed external index')):
                    with self.assertRaises(OSError):
                        execute(db, 'UPDATE t SET v=[0,0];')
                self.assertEqual([], list(Path(root).glob('rows-*.mvb')))
                execute(db, 'UPDATE t SET v=[0,0] WHERE id=2;')
                filename = db.files(db.table('t'))[0]
                self.assertIsNone(journal.reference(filename))
            db.close()
            db = Database(root)
            self.assertEqual([[1, [1.0, 0.0]], [2, [0.0, 0.0]]], rows(db))
            self.assertEqual([[2]], list(execute(db, query)[1]))
            db.close()

    def test_every_truncated_transaction_prefix_is_atomic_across_tables(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db, 'CREATE TABLE u(id BIGINT PRIMARY KEY);')
            execute(db, 'INSERT INTO t VALUES(1,[1,0]);')
            old_end = db.journal.committed_end
            execute(db, 'BEGIN;')
            execute(db, 'INSERT INTO t VALUES(2,[0,0]);')
            execute(db, 'INSERT INTO u VALUES(2);')
            execute(db, 'COMMIT;')
            path = Path(root)/db.journal.name
            complete = path.read_bytes()
            db.close()
            # All cuts include short headers, payloads, checksums, and seals.
            for cut in range(old_end, len(complete)+1):
                path.write_bytes(complete[:cut])
                db = Database(root)
                try:
                    committed = cut == len(complete)
                    self.assertEqual([[1, [1.0, 0.0]]] + ([[2, [0.0, 0.0]]] if committed else []), rows(db), cut)
                    self.assertEqual([[2]] if committed else [], rows(db, 'u'), cut)
                    self.assertEqual(cut if committed else old_end, path.stat().st_size)
                finally:
                    db.close()

    def test_bad_checksum_and_garbage_tail_do_not_publish_partial_commit(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            execute(db, 'INSERT INTO t VALUES(1);')
            prior = db.journal.committed_end
            execute(db, 'INSERT INTO t VALUES(2);')
            path = Path(root)/db.journal.name
            full = path.read_bytes()
            db.close()
            broken = bytearray(full)
            broken[prior+journal.FRAME.size+35] ^= 1
            path.write_bytes(broken)
            db = Database(root)
            self.assertEqual([[1]], rows(db))
            execute(db, 'INSERT INTO t VALUES(3);')
            db.close()
            with path.open('ab') as output:
                output.write(b'\0unfinished-frame'+full[prior:prior+12])
            db = Database(root)
            self.assertEqual([[1], [3]], rows(db))
            db.close()

    def test_failed_stream_preserves_prior_transaction_statement(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            execute(db, 'INSERT INTO t VALUES(0);')
            execute(db, 'BEGIN;')
            execute(db, 'INSERT INTO t VALUES(1);')
            saved = (Path(root)/db.journal.name).stat().st_size
            def broken():
                yield [7]
                raise OSError('failed source')
            with self.assertRaises(OSError):
                db.rewrite('t', broken())
            self.assertEqual(saved, (Path(root)/db.journal.name).stat().st_size)
            self.assertEqual([[0], [1]], rows(db))
            execute(db, 'COMMIT;')
            db.close()
            db = Database(root)
            self.assertEqual([[0], [1]], rows(db))
            db.close()

    def test_failed_commit_barrier_keeps_files_and_requires_restart(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db, 'BEGIN;')
            execute(db, 'INSERT INTO t VALUES(1,[0,0]);')
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            original = os.fsync
            def fail_log(fd):
                # The log is synced last, after external index files and names.
                if os.fstat(fd).st_ino == (Path(root)/db.journal.name).stat().st_ino:
                    raise OSError('failed commit sync')
                original(fd)
            with patch.object(journal.os, 'fsync', fail_log), self.assertRaises(OSError):
                execute(db, 'COMMIT;')
            self.assertTrue(db.journal.poisoned)
            with self.assertRaises(OSError):
                execute(db, 'INSERT INTO t VALUES(2,[1,0]);')
            db.close()
            db = Database(root)
            # A fully written but unacknowledged commit is allowed to recover.
            self.assertEqual([[1, [0.0, 0.0]]], rows(db))
            db.query_cache_limit = 0
            self.assertEqual([[1]], list(execute(db, 'SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1;')[1]))
            db.close()

    def test_checkpoint_compacts_dead_rows_keeps_index_offsets_and_rollbacks(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(4096),v VECTOR(2));')
            db.insert('t', [[i, 'x'*4096, [float(i), 0.0]] for i in range(80)])
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            old_name = db.journal.name
            with patch.object(journal, 'CHECKPOINT_MIN', 1), patch.object(journal, 'CHECKPOINT_GARBAGE', 1):
                execute(db, 'BEGIN;')
                execute(db, "UPDATE t SET s='new';")
                self.assertEqual(old_name, db.journal.name)
                execute(db, 'ROLLBACK;')
                self.assertEqual('x'*4096, rows(db)[0][1])
                execute(db, "UPDATE t SET s='new';")
                self.assertNotEqual(old_name, db.journal.name)
            self.assertEqual([db.journal.name], [p.name for p in Path(root).glob('journal-*.mvj')])
            self.assertLess((Path(root)/db.journal.name).stat().st_size, 20000)
            db.query_cache_limit = 0
            query = 'SELECT id FROM t WHERE id>=3 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 3;'
            self.assertEqual([[3], [4], [5]], list(execute(db, query)[1]))
            db.close()
            db = Database(root)
            self.assertEqual(80, len(rows(db)))
            self.assertEqual([[3], [4], [5]], list(execute(db, query)[1]))
            db.close()

    def test_legacy_catalog_migrates_without_rewriting_existing_segments(self):
        with tempfile.TemporaryDirectory() as root:
            columns = [{'name': 'id', 'kind': 'BIGINT', 'primary': True},
                       {'name': 'v', 'kind': 'VECTOR', 'size': 2, 'primary': False}]
            path = Path(root)/'rows-old.mvb'
            segments.write(path, columns, [[1, [1.0, 0.0]]])
            catalog = {'t': {'columns': columns, 'file': path.name, 'indexes': {}}}
            (Path(root)/'catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
            original = path.read_bytes()
            db = Database(root)
            execute(db, 'INSERT INTO t VALUES(2,[0,0]);')
            self.assertEqual(original, path.read_bytes())
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            db.close()
            db = Database(root)
            self.assertEqual([[1, [1.0, 0.0]], [2, [0.0, 0.0]]], rows(db))
            self.assertEqual([1, 2], list(db.primary_keys(db.table('t'))))
            db.close()

    def test_checkpoint_process_death_and_repeated_recovery(self):
        worker = '''
import sys
from minivector import journal
from minivector.engine import Database
db=Database(sys.argv[1])
stage=sys.argv[2]
journal.CHECKPOINT_MIN=0
journal.CHECKPOINT_GARBAGE=0
def pause():
    print('CUT',flush=True)
    sys.stdin.readline()
if stage in ('before-pointer','after-pointer'):
    original=journal.os.replace
    def interrupted(*args):
        if stage=='before-pointer': pause()
        original(*args)
        if stage=='after-pointer': pause()
    journal.os.replace=interrupted
else:
    original=journal.Journal.commit
    def interrupted(self,catalog):
        if stage=='before-commit': pause()
        original(self,catalog)
        if stage=='after-commit': pause()
    journal.Journal.commit=interrupted
db.journal.checkpoint(db.catalog)
'''
        for stage in ('before-commit', 'after-commit', 'before-pointer', 'after-pointer'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                db = Database(root)
                execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
                execute(db, 'INSERT INTO t VALUES(1),(2);')
                db.close()
                process = subprocess.Popen([sys.executable, '-u', '-c', worker, root, stage],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding='utf-8')
                events = queue.Queue()
                reader = threading.Thread(target=lambda: events.put(process.stdout.readline().strip()), daemon=True)
                reader.start()
                try:
                    self.assertEqual('CUT', events.get(timeout=10))
                finally:
                    process.kill()
                    process.wait(timeout=10)
                    reader.join(timeout=2)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
                for _ in range(2):
                    db = Database(root)
                    self.assertEqual([[1], [2]], rows(db))
                    db.close()

    def test_recovery_can_be_killed_repeatedly_before_tail_repair(self):
        worker = '''
import sys
from minivector import journal
from minivector.engine import Database
original=journal.Journal.recover
def interrupted(self):
    result=original(self)
    print('CUT',flush=True)
    sys.stdin.readline()
    return result
journal.Journal.recover=interrupted
Database(sys.argv[1])
'''
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            execute(db, 'INSERT INTO t VALUES(1);')
            execute(db, 'BEGIN;')
            execute(db, 'INSERT INTO t VALUES(2);')
            path = Path(root)/db.journal.name
            unfinished = path.read_bytes()
            committed_end = db.journal.committed_end
            db.close()
            path.write_bytes(unfinished+b'\0incomplete')
            for _ in range(2):
                process = subprocess.Popen([sys.executable, '-u', '-c', worker, root],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding='utf-8')
                events = queue.Queue()
                reader = threading.Thread(target=lambda: events.put(process.stdout.readline().strip()), daemon=True)
                reader.start()
                try:
                    self.assertEqual('CUT', events.get(timeout=10))
                finally:
                    process.kill()
                    process.wait(timeout=10)
                    reader.join(timeout=2)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
            db = Database(root)
            self.assertEqual([[1]], rows(db))
            self.assertEqual(committed_end, path.stat().st_size)
            execute(db, 'INSERT INTO t VALUES(3);')
            db.close()
            db = Database(root)
            self.assertEqual([[1], [3]], rows(db))
            db.close()


if __name__ == '__main__':
    unittest.main()
