"""Independent Linux memory-cap probe, not a formal platform resource result."""
import argparse
import json
import resource
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minivector.engine import Database
from minivector.sql import execute


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True, help='new isolated directory')
    parser.add_argument('--load-via-sql', action='store_true',
                        help='exercise SQL parsing and 100-row autocommit INSERT batches')
    parser.add_argument('--with-vector-index', action='store_true',
                        help='build, query, maintain and reopen the disk vector index')
    parser.add_argument('--rows', type=int, default=80000)
    parser.add_argument('--minimum-data-bytes', type=int, default=64*1024*1024)
    args = parser.parse_args()
    root = Path(args.data_dir)
    if root.exists():
        raise SystemExit('resource probe requires a new isolated directory')
    cap = 64 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    db = Database(root)
    extra=',v VECTOR(8)' if args.with_vector_index else ''
    execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(1024)'+extra+');')
    count = args.rows
    if count < 20:
        raise SystemExit('resource probe requires at least20 rows')
    if args.load_via_sql:
        payload = "'" + 'x'*1024 + "'"
        for start in range(0, count, 100):
            vector=",'[0,0,0,0,0,0,0,0]'" if args.with_vector_index else ''
            values = ','.join('(%d,%s%s)' % (i, payload,vector) for i in range(start, min(start+100, count)))
            execute(db, 'INSERT INTO t VALUES ' + values + ';')
    else:
        db.rewrite('t', ([i, 'x'*1024]+([[0.0]*8] if args.with_vector_index else []) for i in range(count)))
    data_bytes = sum((root/file).stat().st_size for file in db.files(db.table('t')))
    assert data_bytes > cap
    assert data_bytes > args.minimum_data_bytes
    vector_query="SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0]') LIMIT 10;"
    if args.with_vector_index:
        execute(db,'CREATE VECTOR INDEX probe_idx ON t(v);')
        for mode in ('exact','ann'):
            assert list(execute(db,vector_query,mode=mode)[1]) == [[i] for i in range(10)]
    headers, rows = execute(db, 'SELECT id FROM t ORDER BY id DESC;')
    actual = 0
    for row in rows:
        assert row == [count-1-actual]
        actual += 1
    assert actual == count
    execute(db, 'BEGIN;')
    vector_update=",v='[2,0,0,0,0,0,0,0]'" if args.with_vector_index else ''
    execute(db, "UPDATE t SET payload='transaction'"+vector_update+" WHERE id < 4;")
    assert list(execute(db,'SELECT payload FROM t WHERE id < 4;')[1]) == [['transaction']]*4
    if args.with_vector_index:
        assert list(execute(db,vector_query,mode='ann')[1]) == [[i] for i in range(4,14)]
    execute(db, 'ROLLBACK;')
    assert list(execute(db,'SELECT payload FROM t WHERE id < 4;')[1]) == [['x'*1024]]*4
    if args.with_vector_index:
        assert list(execute(db,vector_query)[1]) == [[i] for i in range(10)]
    execute(db,'BEGIN;')
    execute(db,'DELETE FROM t WHERE id < 4;')
    execute(db,'COMMIT;')
    db.close()
    db = Database(root)
    assert sum(1 for row in db.rows(db.table('t'))) == count-4
    if args.with_vector_index:
        for mode in ('exact','ann'):
            assert list(execute(db,vector_query,mode=mode)[1]) == [[i] for i in range(4,14)]
    db.close()
    print(json.dumps({'status': 'PASS', 'rows': count, 'table_bytes': data_bytes,
                      'load_path': 'SQL batch INSERT' if args.load_via_sql else 'internal streaming rewrite',
                      'disk_vector_index': args.with_vector_index,
                      'address_space_limit_bytes': cap,
                      'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'remaining_rows': count-4,
                      'verified': 'write, external sort, transaction update/rollback, delete/commit, restart count',
                      'limitations': 'RLIMIT_AS and RSS are not cgroup memory.peak; no formal I/O or latency limits'}))


if __name__ == '__main__': main()
