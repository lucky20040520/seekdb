"""Full workload using the unmodified published JSONL transport and config.

Database container is separate from reference generation and all scalar truth.
This is not the platform benchmark, baseline, score or formal29-case admission.
"""
import argparse
import json
from pathlib import Path
import random
import statistics
import sys
import time

from benchmark import reference_records, verify_records, verify_answer
from benchmark_stdio import counters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'protocol_check'))
from _transport import Session, strict_json


def latency_summary(values):
    ordered = sorted(values)
    return {'count': len(values), 'max_seconds': max(values),
            'median_seconds': statistics.median(values),
            'p95_seconds': ordered[min(len(values)-1, int(len(values)*0.95))]}


def run_round(args, config, number):
    reference = reference_records(args.rows, args.dimensions)
    batches = []
    for start in range(0, args.rows, 100):
        batches.append('INSERT INTO t VALUES ' + ','.join(
            '(%d,%d,\'%s\')' % (row[0], row[1], json.dumps(row[2]))
            for row in reference[start:start+100]) + ';')
    rng, specs, queries = random.Random(29017), [], []
    for index in range(args.queries):
        vector = [rng.uniform(-1, 1) for _ in range(args.dimensions)]
        tag = index % 10 if index % 2 else None
        specs.append((vector, tag))
        queries.append("SELECT id,L2_DISTANCE(v,'%s') AS d FROM t%s ORDER BY d LIMIT 10;" %
                       (json.dumps(vector), '' if tag is None else ' WHERE tag=%d' % tag))
    session = Session(config, args.output.with_name(args.output.stem+'-r%d.stderr.log' % number),
                      image=args.image)
    record = {'round': number, 'rows': args.rows, 'dimensions': args.dimensions,
              'queries': args.queries, 'mode': 'exact', 'half_filtered': True,
              'transport': 'unmodified protocol_check/_transport.py',
              'config': config, 'write_batch_size': 100, 'explicit_batch_commit': True}
    write_latencies, query_latencies = [], []
    def request(sql, samples=None):
        started = time.perf_counter()
        response = session.request(sql, mode='exact')
        elapsed = time.perf_counter()-started
        assert response['ok'] is True, response
        assert elapsed < config['request_timeout'], 'request exceeded single-request time limit'
        if samples is not None:
            samples.append(elapsed)
        return response
    def stored_records():
        # A whole-table vector result could exceed the official16MiB message
        # cap. Page validation only, without modifying its config or SQL truth.
        page = min(1000, max(1, config['max_response_bytes']//(args.dimensions*32+256)))
        for start in range(0, args.rows, page):
            response = request('SELECT * FROM t WHERE id>=%d AND id<%d ORDER BY id;' %
                               (start, start+page))
            assert response['columns'] == ['id', 'tag', 'v']
            yield from response['rows']
    try:
        started = time.perf_counter()
        session.start()
        record['write_startup_seconds'] = time.perf_counter()-started
        request('CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(%d));' % args.dimensions)
        started = time.perf_counter()
        for batch in batches:
            request('BEGIN;', write_latencies)
            assert request(batch, write_latencies)['affected_rows'] == 100
            request('COMMIT;', write_latencies)
        record['write_seconds'] = time.perf_counter()-started
        record['write_latency'] = latency_summary(write_latencies)
        record['write_resources'] = counters(session.name)
        verify_records(stored_records(), reference)
        session.shutdown()

        started = time.perf_counter()
        session.start()
        record['restart_seconds'] = time.perf_counter()-started
        verify_records(stored_records(), reference)
        started = time.perf_counter()
        request(queries[0])
        record['warmup_seconds'] = time.perf_counter()-started
        answers = []
        started = time.perf_counter()
        for sql in queries:
            response = request(sql, query_latencies)
            assert response['columns'] == ['id', 'd']
            answers.append(response['rows'])
        record['query_seconds'] = time.perf_counter()-started
        record['query_latency'] = latency_summary(query_latencies)
        record['query_resources'] = counters(session.name)
        record['session_elapsed_before_shutdown_seconds'] = config['case_timeout'] - (session.deadline-time.monotonic())
        session.shutdown()
    finally:
        session.close()
    groups = {tag: [row for row in reference if row[1] == tag] for tag in range(10)}
    started = time.perf_counter()
    for index, (vector, tag) in enumerate(specs):
        verify_answer(answers[index], reference if tag is None else groups[tag], vector)
    record.update(verification_seconds=time.perf_counter()-started,
                  verified_queries=args.queries,
                  verified_records_before_and_after_restart=args.rows,
                  recall_unfiltered=1.0, recall_filtered=1.0)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--dimensions', type=int, default=128)
    parser.add_argument('--queries', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert args.rows > 0 and args.rows % 100 == 0 and args.queries >= 2
    assert args.dimensions > 0 and args.rounds > 0
    config = strict_json((ROOT/'protocol_check/config.json').read_text(encoding='utf-8'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for number in range(args.rounds):
        record = run_round(args, config, number)
        records.append(record)
        print(json.dumps(record, allow_nan=False), flush=True)
        result = {'rounds': records,
                  'median_write_seconds': statistics.median(r['write_seconds'] for r in records),
                  'median_query_seconds': statistics.median(r['query_seconds'] for r in records),
                  'scope': 'Official JSONL transport/config, custom full workload, all-query scalar oracle',
                  'limitations': 'No official benchmark seed/baseline/E/rank. Host cache not reset. cgroup cat helper included in peaks; shared-device I/O attribution not verified. Write/query snapshots before shutdown; correct COMMIT fsync is relied on, not power-loss tested. Case deadline covers SQL/restart/validation; oracle runs after DB shutdown outside container.'}
        args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
