"""Observe Flat builds and immutable segment reuse across append and UPDATE."""
from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector import flat_index
from minivector.engine import Database
from minivector.segments import Reader
from minivector.sql import execute


def mapping(db):
    return dict(db.table('t')['indexes']['vi']['segments'])


def facts(db, filename):
    data = (db.path / filename).read_bytes()
    magic, dimension, column = flat_index.HEADER.unpack_from(data)
    assert magic == flat_index.MAGIC and dimension == 2
    width = flat_index.LOCATION.size + 4 * dimension
    assert (len(data) - flat_index.HEADER.size) % width == 0
    return {'bytes': len(data), 'records': (len(data) - flat_index.HEADER.size) // width,
            'sha256': hashlib.sha256(data).hexdigest()}


def observed(db, sql):
    builds = []
    original_build, original_row = flat_index.build, Reader._row

    def build(directory, row_file, table, column, sync=True):
        keys = []

        def read(reader):
            row = original_row(reader)
            keys.append(row[0])
            return row

        with patch.object(Reader, '_row', read):
            name = original_build(directory, row_file, table, column, sync)
        builds.append({'source_segment': row_file, 'keys_read': keys, 'sync_argument': sync,
                       'index': facts(db, name)})
        return name

    before = mapping(db)
    original_fsync = os.fsync
    with patch.object(flat_index, 'build', build), patch.object(os, 'fsync', wraps=original_fsync) as sync:
        affected = execute(db, sql)
    sync_count = sync.call_count
    after = mapping(db)
    with patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk:
        _, rows = execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')
        top = list(rows)
    assert affected == 1 and disk.call_count == 1
    return {'builds': builds, 'before': before, 'after': after, 'top1': top,
            'fsync_calls_during_statement': sync_count,
            'index_records': sorted(facts(db, name)['records'] for name in after.values())}


def rollback_migration(root):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        execute(db, 'INSERT INTO t VALUES (1,[1,0]),(2,[2,0]);')
        execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
        execute(db, 'INSERT INTO t VALUES (3,[0,0]);')
        db.query_cache_limit = 0
        before = mapping(db)
        before_hashes = {name: facts(db, name)['sha256'] for name in before.values()}
        assert len(before) == 2
        execute(db, 'BEGIN;')
        update = observed(db, 'UPDATE t SET v=[0,0] WHERE id=1;')
        fresh, = set(update['after'].values()) - set(before.values())
        assert len(update['builds']) == 1 and update['builds'][0]['keys_read'] == [1, 2, 3]
        assert update['top1'] == [[1, 0.0]] and update['index_records'] == [3]
        old_files_intact = all(facts(db, name)['sha256'] == digest for name, digest in before_hashes.items())
        new_exists = (db.path / fresh).exists()
        assert old_files_intact and new_exists
        original_fsync = os.fsync
        with patch.object(os, 'fsync', wraps=original_fsync) as rollback_sync:
            execute(db, 'ROLLBACK;')
        assert update['fsync_calls_during_statement'] == 0 and rollback_sync.call_count == 0
        assert mapping(db) == before
        with patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk:
            top = list(execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1])
        assert disk.call_count == 1 and top == [[3, 0.0]]
        removed = not (db.path / fresh).exists()
        assert removed and all(facts(db, name)['sha256'] == digest for name, digest in before_hashes.items())
        rows = list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1])
        assert rows == [[1, [1.0, 0.0]], [2, [2.0, 0.0]], [3, [0.0, 0.0]]]
    with closing(Database(root)) as db:
        db.query_cache_limit = 0
        assert list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1]) == rows
        assert list(execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1]) == top
    return {'transaction': update, 'old_index_hashes_intact_during_transaction': old_files_intact,
            'new_index_existed': new_exists, 'mapping_restored': True,
            'rollback_fsync_calls': rollback_sync.call_count,
            'new_index_removed_after_rollback': removed, 'top1_after_rollback': top,
            'rows_after_rollback': rows, 'reopened_matches': True}


def batch_layout(root, batch_size):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
        for start in range(1, 101, batch_size):
            db.insert('t', [[key, [key, 0]] for key in range(start, min(start + batch_size, 101))])
        db.query_cache_limit = 0
        descriptor = mapping(db)
        index_bytes = sum(facts(db, name)['bytes'] for name in descriptor.values())
        original_open, original_at, original_candidates = Path.open, Reader.at, flat_index.candidates
        results = []
        for k in (10, 1):
            counts = {'index_opens': 0, 'candidates': 0, 'rows_fetched': 0}

            def opened(path, *args, **kwargs):
                if path.suffix == '.flat':
                    counts['index_opens'] += 1
                return original_open(path, *args, **kwargs)

            def fetched(reader, offset):
                counts['rows_fetched'] += 1
                return original_at(reader, offset)

            def candidates(*args, **kwargs):
                for item in original_candidates(*args, **kwargs):
                    counts['candidates'] += 1
                    yield item

            with patch.object(Path, 'open', opened), patch.object(Reader, 'at', fetched), \
                    patch.object(flat_index, 'candidates', candidates):
                actual = list(execute(db, 'SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT ' + str(k) + ';')[1])
            assert actual == [[key] for key in range(1, k + 1)]
            assert counts == {'index_opens': len(descriptor), 'candidates': 100, 'rows_fetched': k}
            results.append(dict(counts, k=k, ids=[row[0] for row in actual]))
        return {'batch_size': batch_size, 'rows': 100, 'index_segments': len(descriptor),
                'logical_index_bytes': index_bytes, 'queries': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('initial', 'rollback', 'batch-layout'), default='initial', nargs='?')
    args = parser.parse_args()
    if args.stage == 'batch-layout':
        with tempfile.TemporaryDirectory() as root:
            for batch_size in (100, 1):
                print(json.dumps(batch_layout(Path(root) / str(batch_size), batch_size)))
        raise SystemExit(0)
    if args.stage == 'rollback':
        with tempfile.TemporaryDirectory() as root:
            print(json.dumps(rollback_migration(root)))
        raise SystemExit(0)
    with tempfile.TemporaryDirectory() as root:
        with closing(Database(root)) as db:
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db, 'INSERT INTO t VALUES (1,[1,0]),(2,[2,0]);')
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            db.query_cache_limit = 0
            old_segment, = mapping(db)
            old_index = mapping(db)[old_segment]
            before_facts = facts(db, old_index)
            append = observed(db, 'INSERT INTO t VALUES (3,[0,0]);')
            assert append['after'][old_segment] == old_index
            assert facts(db, old_index) == before_facts
            assert len(append['builds']) == 1 and append['builds'][0]['keys_read'] == [3]
            assert append['index_records'] == [1, 2] and append['top1'] == [[3, 0.0]]
            append['old_index_hash_unchanged'] = True
            append['old_index_records'] = before_facts['records']
            update = observed(db, 'UPDATE t SET v=[0,0] WHERE id=1;')
            assert not set(update['before']).intersection(update['after'])
            assert len(update['builds']) == 1 and update['builds'][0]['keys_read'] == [1, 2, 3]
            assert update['index_records'] == [3] and update['top1'] == [[1, 0.0]]
            final_rows = list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1])
        with closing(Database(root)) as db:
            db.query_cache_limit = 0
            assert list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1]) == final_rows
            assert list(execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1]) == [[1, 0.0]]
        print(json.dumps({'append': append, 'update': update, 'reopened_matches': True}))
