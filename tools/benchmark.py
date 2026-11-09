"""Reproducible local paired workload. No claim of official T/B/M or rank."""
import argparse
import hashlib
import json
import heapq
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from struct import pack, unpack


def reference_records(count, dimensions):
    rng = random.Random(19017)
    return [[i, i % 10, [unpack('<f', pack('<f', rng.uniform(-1, 1)))[0]
                         for _ in range(dimensions)]] for i in range(count)]


def verify_records(actual, reference):
    seen = set()
    for row in actual:
        key = row[0]
        assert type(key) is int and 0 <= key < len(reference), 'invalid stored ID'
        assert key not in seen, 'duplicate stored ID'
        assert row == reference[key], 'stored content mismatch at ID %d' % key
        seen.add(key)
    assert len(seen) == len(reference), 'missing stored records'


def verify_answer(answer, records, vector):
    query = [unpack('<f', pack('<f', value))[0] for value in vector]
    expected = heapq.nsmallest(10, (
        (math.sqrt(math.fsum((a-b)**2 for a,b in zip(row[2], query))), row[0])
        for row in records))
    assert [key for distance,key in expected] == [row[0] for row in answer], 'oracle IDs mismatch'
    for (distance,key), row in zip(expected, answer):
        assert math.isclose(distance, row[1], rel_tol=1e-12, abs_tol=1e-12), 'oracle distance mismatch'


def worker(args):
    sys.path.insert(0, str(Path(args.code_root).resolve()))
    from minivector.engine import Database
    from minivector.sql import execute
    rng = random.Random(19017)
    data = Path(args.data_root)
    if data.exists():
        raise SystemExit('use a new data directory for each worker')
    db = Database(data)
    execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(%d));'%args.dimensions)
    write_started = time.perf_counter()
    for start in range(0,args.rows,100):
        values=[]
        for i in range(start,min(start+100,args.rows)):
            vector=[rng.uniform(-1,1) for _ in range(args.dimensions)]
            values.append('(%d,%d,[%s])'%(i,i%10,','.join(map(repr,vector))))
        execute(db,'INSERT INTO t VALUES '+','.join(values)+';')
    write_seconds=time.perf_counter()-write_started
    # Independent deterministic regeneration: do not derive truth from DB output.
    reference = reference_records(args.rows, args.dimensions)
    verify_records(db.rows(db.table('t')), reference)
    db.close()
    db=Database(data)
    verify_records(db.rows(db.table('t')), reference)
    queries=[]
    specifications=[]
    for i in range(args.queries):
        vector=[rng.uniform(-1,1) for _ in range(args.dimensions)]
        where='' if i%2==0 else ' WHERE tag=%d'%(i%10)
        specifications.append((vector,None if i%2==0 else i%10))
        queries.append('SELECT id,L2_DISTANCE(v,[%s]) AS d FROM t%s ORDER BY d LIMIT 10;'%
                       (','.join(map(repr,vector)),where))
    # Warmup is explicitly separate from timed queries; cache build is not hidden
    # in resource accounting (we report it separately, no synthetic B/M score).
    prepare_started=time.perf_counter()
    if args.with_vector_index:
        execute(db,'CREATE VECTOR INDEX bench_idx ON t(v);')
    list(execute(db,queries[0])[1])
    prepare_seconds=time.perf_counter()-prepare_started
    answers=[]
    query_started=time.perf_counter()
    for query in queries:
        answers.append(list(execute(db,query)[1]))
    query_seconds=time.perf_counter()-query_started
    # Independent scalar oracle outside the timed sequence. Check a deterministic
    # spread of filtered and unfiltered queries, including both endpoints.
    indexes=(list(range(len(queries))) if args.verify_all else
             sorted(set(round(i*(len(queries)-1)/max(1,args.verify_samples-1))
                        for i in range(min(args.verify_samples,len(queries))))))
    groups = {tag: [row for row in reference if row[1] == tag] for tag in range(10)}
    verify_started = time.perf_counter()
    for index in indexes:
        vector,tag=specifications[index]
        verify_answer(answers[index], reference if tag is None else groups[tag], vector)
        if args.verify_all and (index+1) % 100 == 0:
            print('Independent oracle verified %d/%d queries' % (index+1, len(queries)),
                  file=sys.stderr, flush=True)
    verification_seconds = time.perf_counter() - verify_started
    cache_bytes=getattr(db,'query_cache_bytes',0)
    db.close()
    source_root=Path(args.code_root)/'minivector'
    source_hash=hashlib.sha256(b''.join(path.read_bytes() for path in sorted(source_root.glob('*.py')))).hexdigest()
    return dict(write_seconds=write_seconds,query_seconds=query_seconds,prepare_seconds=prepare_seconds,
                data_bytes=sum(path.stat().st_size for path in data.iterdir() if path.is_file()),
                cache_bytes=cache_bytes,source_sha256=source_hash,oracle_queries=len(indexes),
                verified_all_queries=len(indexes)==args.queries,
                verified_records_before_and_after_restart=args.rows,
                verification_seconds=verification_seconds,
                with_vector_index=args.with_vector_index,answers=answers)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=2000)
    parser.add_argument('--queries',type=int,default=60)
    parser.add_argument('--dimensions',type=int,default=128)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--verify-samples',type=int,default=20)
    parser.add_argument('--verify-all',action='store_true')
    parser.add_argument('--code-root',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--data-root',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--with-vector-index',action='store_true')
    args=parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args),allow_nan=False))
        return
    records=[]
    for i in range(args.rounds):
        command=[sys.executable,str(Path(__file__).resolve()),'--worker','--rows',str(args.rows),
                 '--queries',str(args.queries),'--dimensions',str(args.dimensions),
                 '--verify-samples',str(args.verify_samples),
                 '--code-root',args.code_root,'--data-root',args.data_root+'-r'+str(i),
                 '--output',args.output]
        if args.with_vector_index:
            command.append('--with-vector-index')
        if args.verify_all:
            command.append('--verify-all')
        env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',PYTHONIOENCODING='utf-8')
        result=subprocess.run(command,capture_output=True,text=True,env=env)
        if result.returncode:
            sys.stderr.write(result.stderr)
            raise SystemExit('benchmark worker failed; incomplete round excluded')
        record=json.loads(result.stdout)
        records.append(record)
        summary={key:value for key,value in record.items() if key!='answers'}
        print(json.dumps(dict(round=i,**summary)),flush=True)
    output=Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(dict(rows=args.rows,queries=args.queries,dimensions=args.dimensions,
        rounds=records,limitations='Local engine workload, not official protocol/cgroup T/B/M or platform score'),
        ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
