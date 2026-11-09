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
    args = parser.parse_args()
    root = Path(args.data_dir)
    if root.exists():
        raise SystemExit('resource probe requires a new isolated directory')
    cap = 64 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    db = Database(root)
    execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,payload VARCHAR(1024));')
    count = 80000
    if args.load_via_sql:
        payload = "'" + 'x'*1024 + "'"
        for start in range(0, count, 100):
            values = ','.join('(%d,%s)' % (i, payload) for i in range(start, min(start+100, count)))
            execute(db, 'INSERT INTO t VALUES ' + values + ';')
    else:
        db.rewrite('t', ([i, 'x'*1024] for i in range(count)))
    data_bytes = sum((root/file).stat().st_size for file in db.files(db.table('t')))
    assert data_bytes > cap
    headers, rows = execute(db, 'SELECT id FROM t ORDER BY id DESC;')
    actual = 0
    for row in rows:
        assert row == [count-1-actual]
        actual += 1
    assert actual == count
    execute(db, 'BEGIN;')
    execute(db, "UPDATE t SET payload='transaction' WHERE id < 4;")
    assert list(execute(db,'SELECT payload FROM t WHERE id < 4;')[1]) == [['transaction']]*4
    execute(db, 'ROLLBACK;')
    assert list(execute(db,'SELECT payload FROM t WHERE id < 4;')[1]) == [['x'*1024]]*4
    execute(db,'BEGIN;')
    execute(db,'DELETE FROM t WHERE id < 4;')
    execute(db,'COMMIT;')
    db.close()
    db = Database(root)
    assert sum(1 for row in db.rows(db.table('t'))) == count-4
    db.close()
    print(json.dumps({'status': 'PASS', 'rows': count, 'table_bytes': data_bytes,
                      'load_path': 'SQL batch INSERT' if args.load_via_sql else 'internal streaming rewrite',
                      'address_space_limit_bytes': cap,
                      'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'remaining_rows': count-4,
                      'verified': 'write, external sort, transaction update/rollback, delete/commit, restart count',
                      'limitations': 'RLIMIT_AS and RSS are not cgroup memory.peak; no formal I/O or latency limits'}))


if __name__ == '__main__': main()
