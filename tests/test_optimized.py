import os
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minivector.engine import Database, DBError
from minivector.sql import execute


class OptimizedPaths(unittest.TestCase):
    def test_long_quoted_input_preserves_escaping_and_atomic_errors(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE `a``b`(id BIGINT PRIMARY KEY,s VARCHAR(12000),v VECTOR(2));')
            text = "a'b\u6c49\U0001f600" * 2000
            escaped = "'" + text.replace("'", "''") + "'"
            execute(db, "INSERT INTO `a``b` VALUES (1," + escaped + ",'[1.0000000596046448,1e-45]');")
            self.assertEqual([[1,text]], list(execute(db, 'SELECT id,s FROM `a``b`;')[1]))
            for vector in ('[true,0]', '[null,0]', '[1e100,0]', '[NaN,0]', '[Infinity,0]'):
                with self.assertRaises(DBError):
                    execute(db, "INSERT INTO `a``b` VALUES (2,'valid','[0,0]'),(3,'bad','" + vector + "');")
                self.assertEqual([[1]], list(execute(db, 'SELECT id FROM `a``b`;')[1]))

    def test_cached_projection_reuses_only_the_matching_distance(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db, 'INSERT INTO t VALUES (2,[3,4]),(1,[0,0]);')
            for indexed in (False, True):
                if indexed:
                    execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
                result = list(execute(db, 'SELECT id,v,L2_DISTANCE(v,[0,0]) AS d,'
                    'L2_DISTANCE(v,[1,0]) AS other FROM t ORDER BY d LIMIT 2;')[1])
                self.assertEqual([[1,[0.0,0.0],0.0,1.0],
                                  [2,[3.0,4.0],5.0,math.sqrt(20)]], result)
                # Historical ambiguous-alias selection uses the first distance.
                self.assertEqual([[1,0.0,10.0]], list(execute(db,
                    'SELECT id,L2_DISTANCE(v,[0,0]) AS x,L2_DISTANCE(v,[10,0]) AS x '
                    'FROM t ORDER BY x LIMIT 1;')[1]))

    def test_equality_postings_share_cache_budget_and_invalidate(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(2));')
            db.insert('t', [[i, i % 40, [float(i), 0.0]] for i in range(400)])
            def query(tag):
                return list(execute(db, 'SELECT id FROM t WHERE tag=%d '
                    'ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 3;' % tag)[1])
            for tag in range(40):
                self.assertEqual([[tag], [tag+40], [tag+80]], query(tag))
            cache = next(iter(db.query_cache.values()))
            self.assertLessEqual(len(cache.postings), 32)
            self.assertLessEqual(cache.posting_bytes, 1024*1024)
            self.assertEqual(cache.size, db.query_cache_bytes)
            self.assertLessEqual(cache.size, db.query_cache_limit)
            execute(db, 'BEGIN;')
            execute(db, 'UPDATE t SET tag=39,v=[-1,0] WHERE id=0;')
            self.assertEqual([[0], [39], [79]], query(39))
            execute(db, 'ROLLBACK;')
            self.assertEqual([[39], [79], [119]], query(39))
            base_size = cache.size - cache.posting_bytes
            db.query_cache.clear(); db.query_cache_bytes = 0
            db.query_cache_limit = base_size + 128
            self.assertEqual([[39], [79], [119]], query(39))
            cache = next(iter(db.query_cache.values()))
            self.assertEqual(0, len(cache.postings))
            self.assertLessEqual(cache.size, db.query_cache_limit)

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
            self.assertEqual([first], [p.name for p in Path(root).glob('rows-*')])
            self.assertEqual([1], list(db.primary_keys(db.table('t'))))

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
