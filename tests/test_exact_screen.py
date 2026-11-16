import heapq
import math
import random
import struct
import tempfile
import unittest

from minivector.engine import Database
from minivector.sql import execute


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


class ExactScreenContracts(unittest.TestCase):
    def check_queries(self, vectors, queries, counts=(1, 10)):
        dimension = len(vectors[0])
        records = [[len(vectors)-i, i % 2, list(map(f32, vector))]
                   for i, vector in enumerate(vectors)]
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, f'CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR({dimension}));')
            db.insert('t', records)
            for query in queries:
                query = list(map(f32, query))
                distance = ('distance', 'v', query)
                for tag in (None, 1, 9):
                    conditions = [] if tag is None else [('tag', '=', tag)]
                    candidates = [(math.sqrt(math.fsum((x-y)**2 for x, y in zip(row[2], query))),
                                   row[0], row[2]) for row in records if tag is None or row[1] == tag]
                    for k in counts:
                        expected = [[key, value, vector] for value, key, vector
                                    in heapq.nsmallest(k, candidates)]
                        actual = list(db.select('t', [(('column', 'id'), 'id'), (distance, 'd'),
                                                     (('column', 'v'), 'v')], conditions,
                                                order=(('column', 'd'), False), limit=k)[1])
                        self.assertEqual(expected, actual, (dimension, tag, k, query[:3]))
            db.close()

    def test_nearly_equal_norms_need_conservative_screen_and_exact_ranking(self):
        rng = random.Random(33019)
        vectors = []
        for i in range(160):
            row = [rng.uniform(-1, 1) for _ in range(128)]
            scale = (1 + (i-80)*1e-10) / math.sqrt(math.fsum(x*x for x in row))
            vectors.append([x*scale for x in row])
        self.check_queries(vectors, [[0]*128, vectors[0]], (0, 1, 10, 200))

    def test_overflow_subnormal_underflow_and_cancellation(self):
        rng = random.Random(811)
        for scale in (1e-40, 1e-22, 1.0, 1e19, 1e38):
            with self.subTest(scale=scale):
                vectors = [[f32(rng.uniform(-1, 1)*scale) for _ in range(19)] for _ in range(48)]
                vectors.extend([vectors[0][:], [f32(scale)]*19, [-f32(scale)]*19])
                self.check_queries(vectors, [[0]*19, vectors[0], [f32(scale)]*19])
        # Subtraction itself overflows float32, while the reference is finite.
        self.check_queries([[3.4e38], [-3.4e38], [0.0], [1e-45]], [[-3.4e38], [3.4e38]])

    def test_high_dimension_uses_safe_fallback(self):
        vectors = [[float(i)] * 4097 for i in range(-8, 9)]
        self.check_queries(vectors, [[0]*4097, [0.25]*4097])

    def test_dot_screen_random_filters_and_adversarial_magnitudes(self):
        rng = random.Random(92117)
        base = [[f32(rng.uniform(-1, 1)) for _ in range(128)] for _ in range(320)]
        self.check_queries(base, [base[17], [f32(rng.uniform(-1, 1)) for _ in range(128)]])
        for scale, offset in ((1e-40, 0), (1e19, 0), (1e30, 0), (4096, 1e10)):
            with self.subTest(scale=scale, offset=offset):
                vectors = [[f32(x*scale+offset) for x in row] for row in base]
                vectors[-1] = vectors[0][:]
                self.check_queries(vectors, [vectors[0], vectors[19]])

    def test_norm_budget_is_optional_and_large_gather_is_bounded(self):
        from minivector.vector import VectorCache
        rng = random.Random(501)
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(1024));')
            records = [[i, i % 2, [f32(rng.uniform(-1, 1)) for _ in range(1024)]] for i in range(640)]
            db.insert('t', records)
            table = db.table('t')
            cache = db.cached_vectors(table)
            self.assertIsNotNone(cache.norms)
            limited = VectorCache.build(db, table, cache.size-len(records)*8-256)
            self.assertIsNotNone(limited)
            self.assertIsNone(limited.norms)
            query = records[12][2]
            conditions = [('tag', '=', 0)]
            # 320 x 1024 float32 exceeds the 1 MiB gather limit.
            expected = heapq.nsmallest(10, records[::2], key=lambda row:
                (math.sqrt(math.fsum((a-b)**2 for a, b in zip(row[2], query))), row[0]))
            self.assertEqual([row[0] for row in expected],
                             [row[0] for row, _ in cache.topk(conditions, query, 10, False)])
            self.assertEqual(cache.topk(conditions, query, 10), limited.topk(conditions, query, 10))
            self.assertLessEqual(cache.size, cache.budget)
            db.close()

    def test_norm_cache_tracks_transaction_index_and_rollback_visibility(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root)
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(128));')
            db.insert('t', [[i, i % 2, [float(i+1)]*128] for i in range(300)])
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            zeros = '['+','.join(['0']*128)+']'
            query = "SELECT id FROM t WHERE tag=0 ORDER BY L2_DISTANCE(v,'%s') LIMIT 1;" % zeros
            self.assertEqual([[0]], list(execute(db, query)[1]))
            old = next(iter(db.query_cache.values()))
            self.assertIsNotNone(old.norms)
            execute(db, 'BEGIN;')
            execute(db, "UPDATE t SET v='%s' WHERE id=298;" % zeros)
            self.assertEqual([[298]], list(execute(db, query)[1]))
            self.assertIsNot(old, next(iter(db.query_cache.values())))
            execute(db, 'ROLLBACK;')
            self.assertEqual([[0]], list(execute(db, query)[1]))
            execute(db, "UPDATE t SET v='%s' WHERE id=298;" % zeros)
            db.close()
            db = Database(root)
            self.assertEqual([[298]], list(execute(db, query)[1]))
            db.close()


if __name__ == '__main__':
    unittest.main()
