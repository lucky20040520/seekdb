import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.sql import execute


class OptimizedPaths(unittest.TestCase):
    def test_projection_alias_does_not_replace_real_column(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(4));')
            execute(db,"INSERT INTO t VALUES (1,'real');")
            self.assertEqual([[1,'real']],list(execute(db,'SELECT id AS tag,tag AS actual FROM t ORDER BY tag;')[1]))

    def test_directory_sync_error_preserves_published_files(self):
        import minivector.engine as engine
        with tempfile.TemporaryDirectory() as root:
            db=Database(root)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY);')
            calls=[]
            original=engine.sync_directory
            def interrupted(path):
                calls.append(path)
                if len(calls)==2:
                    raise OSError('simulated directory sync failure after replace')
                original(path)
            with patch.object(engine,'sync_directory',interrupted):
                with self.assertRaises(OSError):
                    execute(db,'INSERT INTO t VALUES (1);')
            db.close()
            db=Database(root)
            self.assertEqual([[1]],list(execute(db,'SELECT id FROM t;')[1]))

    def test_insert_is_append_only_and_rollback_safe(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db,'INSERT INTO t VALUES (1,[1,0]);')
            first=db.files(db.table('t'))[0]
            first_bytes=(Path(root)/first).read_bytes()
            execute(db,'BEGIN;')
            execute(db,'INSERT INTO t VALUES (2,[2,0]);')
            self.assertEqual(2,len(db.files(db.table('t'))))
            self.assertEqual(first_bytes,(Path(root)/first).read_bytes())
            execute(db,'ROLLBACK;')
            self.assertEqual([first],db.files(db.table('t')))
            self.assertEqual([[1]],list(execute(db,'SELECT id FROM t;')[1]))
            self.assertEqual(1,len(list(Path(root).glob('rows-*.keys'))))

    def test_key_ranges_keep_duplicate_checks_for_legacy_and_overlapping_segments(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
            execute(db, "INSERT INTO t VALUES (-10), (10);")
            legacy = db.files(db.table("t"))[0]
            db.catalog["t"].pop("key_ranges")
            db.publish(db.catalog)
            (Path(root) / (legacy + ".keys")).unlink()

            execute(db, "INSERT INTO t VALUES (30), (40);")
            db = Database(root)
            for key in (-10, 40):
                with self.assertRaisesRegex(DBError, "duplicate primary key"):
                    execute(db, f"INSERT INTO t VALUES ({key});")
            execute(db, "INSERT INTO t VALUES (35);")
            execute(db, "BEGIN;")
            execute(db, "DELETE FROM t WHERE id=40;")
            execute(db, "INSERT INTO t VALUES (40);")
            execute(db, "ROLLBACK;")
            with self.assertRaisesRegex(DBError, "duplicate primary key"):
                execute(db, "INSERT INTO t VALUES (40);")
            self.assertEqual([[-10], [10], [30], [35], [40]],
                             list(execute(db, "SELECT id FROM t ORDER BY id;")[1]))

    def test_cached_scalar_differential_and_ties(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(3),v VECTOR(128));')
            # Equal norm under component permutations, including large dynamic range.
            base=[1e10,1e-10,3,4]*32
            vectors=[base[i:]+base[:i] for i in range(40)]
            db.insert('t',[[39-i,'yes' if i%3 else 'no',v] for i,v in enumerate(vectors)])
            queries=["SELECT id,L2_DISTANCE(v,[%s]) AS d FROM t %s ORDER BY d LIMIT %d;" %
                     (','.join(map(str,q)),where,k)
                     for q in [[0]*128,base,[2]*128]
                     for where in ['',"WHERE tag='yes'",'WHERE id>=20',"WHERE tag='xxx'"]
                     for k in [0,1,10,100]]
            for query in queries:
                fast=list(execute(db,query)[1])
                db.query_cache.clear(); db.query_cache_bytes=0
                with patch.dict(os.environ,MINIVECTOR_NO_NUMPY='1'):
                    scalar=list(execute(db,query)[1])
                self.assertEqual(scalar,fast,query)

    def test_cache_invalidation_and_budget(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root)
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db,'INSERT INTO t VALUES (1,[1,0]),(2,[2,0]);')
            query='SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1;'
            self.assertEqual([[1]],list(execute(db,query)[1]))
            execute(db,'BEGIN;')
            execute(db,'UPDATE t SET v=[0,0] WHERE id=2;')
            self.assertEqual([[2]],list(execute(db,query)[1]))
            execute(db,'ROLLBACK;')
            self.assertEqual([[1]],list(execute(db,query)[1]))
            db.query_cache.clear(); db.query_cache_bytes=0; db.query_cache_limit=1
            self.assertEqual([[1]],list(execute(db,query)[1]))
            self.assertEqual({},db.query_cache)


if __name__=='__main__':unittest.main()
