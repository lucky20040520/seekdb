"""Diagnostic factorial: reuse/build versus batch/scalar, not platform scoring."""
import argparse
from contextlib import closing
import heapq
import json
import math
from pathlib import Path
import random
import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from minivector import journal
from minivector.engine import Database
from minivector.segments import Reader
from minivector.sql import execute
from minivector.vector import VectorCache


N, DIM, QUERY_COUNT, K, SEED = 2048, 32, 80, 10, 65021
GROUPS = [('reuse-batch', True, True), ('build-batch', False, True),
          ('reuse-scalar', True, False), ('build-scalar', False, False)]


def fixture():
    randomizer = random.Random(SEED)
    rows = [[i + 1, [randomizer.randint(-8, 8) for _ in range(DIM)]] for i in range(N)]
    queries = [[randomizer.randint(-8, 8) for _ in range(DIM)] for _ in range(QUERY_COUNT)]
    return rows, queries


def expected(rows, query):
    # Plain integer arithmetic is independent of NumPy and binary row decoding.
    ranked = heapq.nsmallest(K, ((sum((x - q) ** 2 for x, q in zip(v, query)), pk)
                                for pk, v in rows))
    return [[pk, math.sqrt(distance)] for distance, pk in ranked]


def variant(db, queries, reuse, batch):
    table = db.table('t')
    started = time.perf_counter()
    held = VectorCache.build(db, table, 16 * 1024 * 1024) if reuse else None
    preparation = time.perf_counter() - started
    output, sizes = [], []
    started = time.perf_counter()
    for query in queries:
        cache = held if reuse else VectorCache.build(db, table, 16 * 1024 * 1024)
        assert cache is not None
        if batch:
            distances = cache.squared_distances(None, query, N, cache.np.float64)
        else:
            distances = [math.fsum((float(x) - float(q)) ** 2 for x, q in zip(v, query))
                         for v in cache.vectors]
        ranked = heapq.nsmallest(K, ((float(distance), int(pk))
                                    for distance, pk in zip(distances, cache.keys)))
        output.append([[pk, math.sqrt(distance)] for distance, pk in ranked])
        sizes.append(cache.size)
    duration = time.perf_counter() - started
    return {'preparation_seconds': preparation, 'query_seconds': duration,
            'total_seconds': preparation + duration, 'cache_accounted_bytes': max(sizes),
            'answers': output}


def diagnose(db, queries):
    original_row, original_read = Reader._row, journal.SegmentView.read
    results = []
    for label, reuse, batch in GROUPS:
        counters = {'decoded_rows': 0, 'application_read_bytes': 0}

        def row(reader):
            value = original_row(reader)
            counters['decoded_rows'] += 1
            return value

        def read(source, count=-1):
            data = original_read(source, count)
            counters['application_read_bytes'] += len(data)
            return data

        with patch.object(Reader, '_row', row), patch.object(journal.SegmentView, 'read', read), \
                patch.object(VectorCache, 'build', wraps=VectorCache.build) as builds:
            result = variant(db, queries, reuse, batch)
        results.append({'group': label, 'queries': len(queries), 'build_calls': builds.call_count,
                        **counters, 'cache_accounted_bytes': result['cache_accounted_bytes'],
                        'answers': result['answers']})
    return results


def seed(path, rows):
    with closing(Database(path)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(32));')
        for start in range(0, N, 128):
            execute(db, 'INSERT INTO t VALUES ' + ','.join(
                '(' + str(pk) + ',' + json.dumps(v) + ')' for pk, v in rows[start:start + 128]) + ';')


def lifecycle(root, counts=(1, 80)):
    rows, queries = fixture()
    base = root / 'base'
    seed(base, rows)
    query_file = root / 'queries.json'
    query_file.write_text(json.dumps(queries), encoding='utf-8')
    truths = {count: [expected(rows, query) for query in queries[:count]] for count in counts}
    for round_number in range(1, 4):
        order = GROUPS if round_number % 2 else list(reversed(GROUPS))
        for count in counts:
            for label, _, _ in order:
                target = root / ('r%d-q%d-%s' % (round_number, count, label))
                shutil.copytree(base, target)
                command = [sys.executable, '-m', 'tools.verify_reuse_factors', 'worker',
                           '--data-dir', str(target), '--group', label,
                           '--queries-file', str(query_file), '--query-count', str(count)]
                started = time.perf_counter()
                child = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       encoding='utf-8', timeout=120,
                                       env=dict(os.environ, PYTHONIOENCODING='utf-8'))
                elapsed = time.perf_counter() - started
                assert child.returncode == 0, child.stderr
                output = json.loads(child.stdout.splitlines()[-1])
                assert output.pop('answers') == truths[count]
                print(json.dumps({'round': round_number, 'queries': count, 'group': label,
                                  'process_wall_seconds': elapsed, 'exit_code': child.returncode,
                                  'stderr': child.stderr, **output}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'diagnose', 'bench', 'worker', 'lifecycle', 'lifecycle-four'), default='prepare', nargs='?')
    parser.add_argument('--data-dir')
    parser.add_argument('--queries-file')
    parser.add_argument('--query-count', type=int, choices=(1, 4, 80), default=80)
    parser.add_argument('--group', choices=[group[0] for group in GROUPS])
    args = parser.parse_args()
    print(json.dumps({'rows': N, 'dimensions': DIM, 'queries': QUERY_COUNT, 'k': K, 'seed': SEED,
                      'groups': GROUPS, 'scope': 'local kernel/build microbenchmark; physical device IO unknown'}))
    if args.stage == 'worker':
        if not args.data_dir or not args.queries_file or not args.group:
            parser.error('worker requires data, queries and group')
        queries = json.loads(Path(args.queries_file).read_text(encoding='utf-8'))[:args.query_count]
        _, reuse, batch = next(group for group in GROUPS if group[0] == args.group)
        with closing(Database(args.data_dir)) as db:
            result = variant(db, queries, reuse, batch)
        print(json.dumps(result), flush=True)
    elif args.stage in ('lifecycle', 'lifecycle-four'):
        with tempfile.TemporaryDirectory() as temporary:
            lifecycle(Path(temporary), counts=(4,) if args.stage == 'lifecycle-four' else (1, 80))
    elif args.stage in ('diagnose', 'bench'):
        rows, queries = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / 'data'
            seed(data, rows)
            with closing(Database(data)) as db:
                selected = queries[:4] if args.stage == 'diagnose' else queries
                truth = [expected(rows, query) for query in selected]
                if args.stage == 'diagnose':
                    results = diagnose(db, selected)
                    for result in results:
                        assert result.pop('answers') == truth
                        result['verified_queries'] = len(truth)
                        print(json.dumps(result))
                else:
                    # Library initialization is excluded symmetrically from kernel timings.
                    __import__('numpy')
                    for round_number in range(1, 4):
                        order = GROUPS if round_number % 2 else list(reversed(GROUPS))
                        for label, reuse, batch in order:
                            result = variant(db, selected, reuse, batch)
                            assert result.pop('answers') == truth
                            print(json.dumps({'round': round_number, 'group': label,
                                              'verified_queries': len(truth), **result}), flush=True)
