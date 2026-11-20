"""Observe repeated-column conjunctions and pre-ranking candidate membership."""
import argparse
from collections import OrderedDict
from contextlib import closing
import json
import math
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector.engine import Database
from minivector.sql import execute
from minivector.vector import VectorCache


ROWS = [(1, 1, 0, [0, 0]), (2, 0, 1, [0, 0]), (3, 1, 1, [3, 0]),
        (4, 1, 1, [0, 3]), (5, 1, 1, [4, 0]), (6, 2, 1, [0, 0])]
CASES = [('both', 'a=1 AND b=1', [('a', 1), ('b', 1)], 2),
         ('reversed', 'b=1 AND a=1', [('b', 1), ('a', 1)], 2),
         ('contradiction', 'a=1 AND a=2', [('a', 1), ('a', 2)], 2),
         ('repeated', 'a=1 AND a=1 AND b=1', [('a', 1), ('a', 1), ('b', 1)], 2),
         ('larger-k', 'a=1 AND b=1', [('a', 1), ('b', 1)], 10)]


def truth(conditions, count, query=(0, 0)):
    matches = []
    for primary, a, b, vector in ROWS:
        fields = {'a': a, 'b': b}
        def accepts(condition):
            if len(condition) == 2:
                name, value = condition
                return fields[name] == value
            name, comparison, value = condition
            if comparison == '>=':
                return fields[name] >= value
            if comparison == '<=':
                return fields[name] <= value
            raise AssertionError(comparison)

        if all(accepts(condition) for condition in conditions):
            distance = math.sqrt(math.fsum((float(v) - float(q)) ** 2 for v, q in zip(vector, query)))
            matches.append([primary, distance])
    return sorted(matches, key=lambda row: (row[1], row[0]))[:count]


def run(root, reuse=False, ranges=False):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,a BIGINT,b BIGINT,v VECTOR(2));')
        values = ','.join('(%d,%d,%d,%s)' % (pk, a, b, json.dumps(v)) for pk, a, b, v in ROWS)
        execute(db, 'INSERT INTO t VALUES ' + values + ';')
        results = []
        original = VectorCache.candidate_indexes
        original_distances = VectorCache.squared_distances
        cases = [case + ([0, 0],) for case in CASES]
        if reuse:
            cases.append(('changed-query', 'a=1 AND b=1', [('a', 1), ('b', 1)], 1, [4, 0]))
        if ranges:
            for label in ('range-first', 'range-again'):
                cases.append((label, 'a>=1 AND a<=1 AND b=1',
                              [('a', '>=', 1), ('a', '<=', 1), ('b', 1)], 3, [4, 0]))
        for label, condition, reference, count, query_vector in cases:
            observed = []
            computed = []

            def candidates(cache, conditions):
                signature = tuple(tuple(c) for c in conditions)
                backing = cache.postings
                hit = signature in backing

                class TracedPostings(OrderedDict):
                    lookup_count = 0

                    def __contains__(self, key):
                        self.lookup_count += 1
                        return super().__contains__(key)

                tracked = TracedPostings(backing)
                before_count = len(backing)
                cache.postings = tracked
                try:
                    indexes = original(cache, conditions)
                finally:
                    backing.clear()
                    backing.update(tracked)
                    cache.postings = backing
                keys = cache.keys if indexes is None else cache.keys[indexes]
                observed.append({'conditions': conditions, 'keys': [int(key) for key in keys],
                                 'posting_hit_before': hit,
                                 'all_equality_eligible': all(op == '=' for _, op, _ in conditions),
                                 'posting_contains_callbacks': tracked.lookup_count,
                                 'postings_count_before_after': [before_count, len(backing)],
                                 'scalar_array_lengths': {name: len(cache.scalar[name]) for name, _, _ in conditions}})
                return indexes

            def distances(cache, indexes, query, size, dtype):
                keys = cache.keys if indexes is None else cache.keys[indexes]
                computed.append({'keys': [int(key) for key in keys], 'query': list(map(float, query)),
                                 'size': size, 'dtype': str(dtype)})
                return original_distances(cache, indexes, query, size, dtype)

            sql = 'SELECT id,L2_DISTANCE(v,' + json.dumps(query_vector) + ') AS d FROM t WHERE ' + condition + ' ORDER BY d LIMIT %d;' % count
            with patch.object(VectorCache, 'candidate_indexes', candidates), \
                    patch.object(VectorCache, 'squared_distances', distances):
                rows = list(execute(db, sql)[1])
            expected = truth(reference, count, query_vector)
            assert rows == expected, (label, rows, expected)
            results.append({'label': label, 'sql': sql, 'rows': rows, 'truth': expected,
                            'cache_candidate_calls': observed, 'batch_distance_calls': computed})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run', 'reuse', 'range'), default='prepare', nargs='?')
    args = parser.parse_args()
    print(json.dumps({'rows': ROWS, 'cases': CASES}))
    if args.stage in ('run', 'reuse', 'range'):
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(run(Path(temporary) / 'data', reuse=args.stage in ('reuse', 'range'),
                                 ranges=args.stage == 'range')))
