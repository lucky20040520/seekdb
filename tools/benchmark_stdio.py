"""Full diagnostic-pipe workload; driver/oracle outside measured DB containers.

Requires a built local image and Docker. No network at runtime. cgroup counters
are local observations, not official dedicated-device I/O or frozen scores.
"""
import argparse
import json
from pathlib import Path
import random
import statistics
import subprocess
import time
import uuid

from benchmark import reference_records, verify_records, verify_answer
from run_stdio_public import Session


def docker(*args):
    return subprocess.run(['docker', *args], check=True, capture_output=True,
                          text=True, encoding='utf-8', timeout=120).stdout


def counters(name):
    result = {'scope': 'DB container cgroup including Docker-exec cat helpers; before EOF',
              'physical_io_attribution': 'Unverified shared CI device; raw counters only; no official B'}
    for field, path in [('memory_peak_bytes', '/sys/fs/cgroup/memory.peak'),
                        ('io_stat', '/sys/fs/cgroup/io.stat')]:
        try:
            value = docker('exec', name, 'cat', path).strip()
            result[field] = int(value) if field == 'memory_peak_bytes' else value
        except (subprocess.SubprocessError, ValueError) as error:
            result[field] = None
            result[field + '_unavailable'] = str(error)
    return result


def run_round(args, number):
    token = 'minivector-pipe-' + uuid.uuid4().hex
    volume, names = token + '-data', [token + '-write', token + '-query']
    reference = reference_records(args.rows, args.dimensions)
    batches = []
    for start in range(0, args.rows, 100):
        values = ['(%d,%d,\'%s\')' % (row[0], row[1], json.dumps(row[2]))
                  for row in reference[start:start+100]]
        batches.append('INSERT INTO t VALUES ' + ','.join(values) + ';')
    rng, specifications, queries = random.Random(29017), [], []
    for index in range(args.queries):
        vector = [rng.uniform(-1, 1) for _ in range(args.dimensions)]
        tag = index % 10 if index % 2 else None
        where = '' if tag is None else ' WHERE tag=%d' % tag
        specifications.append((vector, tag))
        queries.append("SELECT id,L2_DISTANCE(v,'%s') AS d FROM t%s ORDER BY d LIMIT 10;" %
                       (json.dumps(vector), where))
    docker('volume', 'create', volume)
    session = None
    record = {'round': number, 'rows': args.rows, 'dimensions': args.dimensions,
              'queries': args.queries, 'mode': 'exact', 'half_filtered': True,
              'prepared_input_outside_timing': True}
    def start(name):
        return Session(None, 'exact', 120, command=[
            'docker', 'run', '--rm', '-i', '--name', name, '--network', 'none',
            '--cpus', '2', '--memory', '2g', '--memory-swap', '2g',
            '--mount', 'type=volume,src=' + volume + ',dst=/data', args.image, '--protocol', 'diagnostic'])
    try:
        started = time.perf_counter()
        session = start(names[0])
        record['write_startup_seconds'] = time.perf_counter()-started
        session.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(%d));' % args.dimensions)
        started = time.perf_counter()
        for sql in batches:
            assert session.execute(sql) == min(100, args.rows), 'write acknowledgement mismatch'
        record['write_seconds'] = time.perf_counter()-started
        record['write_resources'] = counters(names[0])
        columns, rows = session.execute('SELECT * FROM t;')
        assert columns == ['id', 'tag', 'v']
        verify_records(rows, reference)
        session.close()
        session = None

        started = time.perf_counter()
        session = start(names[1])
        record['restart_seconds'] = time.perf_counter()-started
        columns, rows = session.execute('SELECT * FROM t;')
        assert columns == ['id', 'tag', 'v']
        verify_records(rows, reference)
        warmup = time.perf_counter()
        session.execute(queries[0])
        record['warmup_seconds'] = time.perf_counter()-warmup
        answers = []
        started = time.perf_counter()
        for sql in queries:
            columns, rows = session.execute(sql)
            assert columns == ['id', 'd']
            answers.append(rows)
        record['query_seconds'] = time.perf_counter()-started
        record['query_resources'] = counters(names[1])
        session.close()
        session = None

        # All reference work is outside the DB containers and timed workload.
        grouped = {tag: [row for row in reference if row[1] == tag] for tag in range(10)}
        started = time.perf_counter()
        for index, (vector, tag) in enumerate(specifications):
            verify_answer(answers[index], reference if tag is None else grouped[tag], vector)
        record['verification_seconds'] = time.perf_counter()-started
        record.update(verified_queries=args.queries,
                      verified_records_before_and_after_restart=args.rows)
        return record
    finally:
        if session is not None:
            session.abort()
        # Only unique test-owned containers and volume created by this round.
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
        subprocess.run(['docker', 'volume', 'rm', volume], check=True,
                       capture_output=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--dimensions', type=int, default=128)
    parser.add_argument('--queries', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert args.rows > 0 and args.rows % 100 == 0
    assert args.queries > 0 and args.dimensions > 0 and args.rounds > 0
    records = []
    for number in range(args.rounds):
        record = run_round(args, number)
        records.append(record)
        print(json.dumps(record, allow_nan=False), flush=True)
        result = {'rounds': records,
                  'median_write_seconds': statistics.median(r['write_seconds'] for r in records),
                  'median_query_seconds': statistics.median(r['query_seconds'] for r in records),
                  'limitations': 'Own diagnostic wire, not official. Shared CI caches/devices; no cold-cache reset, no dedicated-device attribution, no E/rank. Query container counters include restart/full content check/warmup; no index built.'}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
