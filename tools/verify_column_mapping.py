"""Observe named INSERT mapping with non-leading BIGINT primary key."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch

from minivector import flat_index, journal
from minivector.segments import Reader
from minivector.engine import Database, DBError
from minivector.sql import execute


CREATE = 'CREATE TABLE t(tag BIGINT,v VECTOR(2),id BIGINT PRIMARY KEY,note VARCHAR(16));'
INSERT = "INSERT INTO t(id,note,tag,v) VALUES (7,'left',10,[1,0]),(3,'right',90,[0,1]);"
PROBES = [
    "INSERT INTO t(id,note,tag,v) VALUES (7,'duplicate',11,[3,0]);",
    "INSERT INTO t(id,note,tag,v) VALUES (9,'shared',10,[2,0]);",
]
READ = 'SELECT * FROM t ORDER BY id;'
TOP = 'SELECT id,tag,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;'
ALIASED = 'SELECT tag AS id,id AS tag,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;'
ALL_DISTANCE = 'SELECT id,tag,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 3;'


def query(db, sql):
    columns, rows = execute(db, sql)
    return {'columns': columns, 'rows': list(rows)}


def run(root):
    with closing(Database(root)) as db:
        execute(db, CREATE)
        execute(db, INSERT)
        initial = {'read': query(db, READ), 'top': query(db, TOP)}
        probes = []
        for sql in PROBES:
            try:
                response = {'affected_rows': execute(db, sql)}
            except DBError as exc:
                response = {'code': exc.code, 'message': str(exc)}
            probes.append({'sql': sql, 'response': response, 'read': query(db, READ), 'top': query(db, TOP)})
    with closing(Database(root)) as db:
        reopened = {'read': query(db, READ), 'top': query(db, TOP)}
    return {'initial': initial, 'probes': probes, 'reopened': reopened}


def matrix(root):
    inputs = {
        'named': "INSERT INTO t(id,note,tag,v) VALUES (7,'left',10,[1,0]),(3,'right',90,[0,1]),(9,'shared',10,[2,0]);",
        'declaration': "INSERT INTO t VALUES (10,[1,0],7,'left'),(90,[0,1],3,'right'),(10,[2,0],9,'shared');",
    }
    results = []
    for label, sql in inputs.items():
        path = root / label
        with closing(Database(path)) as db:
            execute(db, CREATE)
            execute(db, sql)
            before = {'read': query(db, READ), 'normal': query(db, TOP),
                      'aliased': query(db, ALIASED), 'all_distance': query(db, ALL_DISTANCE)}
        with closing(Database(path)) as db:
            reopened = {'read': query(db, READ), 'normal': query(db, TOP),
                        'aliased': query(db, ALIASED), 'all_distance': query(db, ALL_DISTANCE)}
        assert before == reopened
        assert before['normal']['rows'] == [[3, 90, 1.0]]
        assert before['aliased']['rows'] == [[90, 3, 1.0]]
        assert before['all_distance']['rows'] == [[3, 90, 1.0], [7, 10, 1.0], [9, 10, 2.0]]
        results.append({'input': label, 'sql': sql, 'before': before, 'reopened': reopened})
    assert results[0]['before'] == results[1]['before']
    return results


def disk_observe(db, sql):
    candidates, fetched, at_rows, sums, cache_results = [], [], [], [], []
    original_candidates, original_read = flat_index.candidates, flat_index.read_row
    original_at, original_sum, original_cache = Reader.at, flat_index.math.fsum, db.cached_vectors

    def scan(*args, **kwargs):
        stream = original_candidates(*args, **kwargs)
        try:
            for location in stream:
                candidates.append(list(location))
                yield location
        finally:
            stream.close()

    def read(directory, location, table):
        row = original_read(directory, location, table)
        pk = next(i for i, c in enumerate(table['columns']) if c['primary'])
        fetched.append({'location': list(location), 'actual_primary': row[pk], 'actual_tag': row[0]})
        return row

    def at(reader, offset):
        row = original_at(reader, offset)
        pk = next(i for i, c in enumerate(reader.columns) if c['primary'])
        at_rows.append([offset, row[pk], row[0]])
        return row

    def summed(values):
        caller = sys._getframe(1)
        numbers = list(values)
        sums.append({'module': caller.f_globals.get('__name__'), 'function': caller.f_code.co_name,
                     'terms': numbers})
        return original_sum(numbers)

    def cache(table):
        value = original_cache(table)
        cache_results.append(value is not None)
        return value

    with patch.object(flat_index, 'candidates', scan), patch.object(flat_index, 'read_row', read), \
            patch.object(Reader, 'at', at), patch.object(flat_index.math, 'fsum', summed), \
            patch.object(db, 'cached_vectors', cache), \
            patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk:
        output = query(db, sql)
    assert disk.call_count == 1 and cache_results == [False]
    assert len(candidates) == 3 and len(fetched) == len(at_rows) == 1
    assert fetched[0]['actual_primary'] == 3 and fetched[0]['actual_tag'] == 90
    assert fetched[0]['location'][1] == fetched[0]['actual_primary']
    return {'sql': sql, 'output': output, 'indexed_rows_calls': disk.call_count,
            'cache_returned_non_none': cache_results, 'cache_entries': len(db.query_cache),
            'candidates': candidates, 'fetched': fetched, 'reader_at': at_rows, 'fsum_calls': sums}


def disk_migration(root):
    inputs = {
        'named': "INSERT INTO t(id,note,tag,v) VALUES (7,'left',10,[1,0]),(3,'right',90,[0,1]),(9,'shared',10,[2,0]);",
        'declaration': "INSERT INTO t VALUES (10,[1,0],7,'left'),(90,[0,1],3,'right'),(10,[2,0],9,'shared');",
    }
    results = []
    for label, sql in inputs.items():
        path = root / label
        with closing(Database(path)) as db:
            execute(db, CREATE)
            execute(db, sql)
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            db.query_cache_limit = 0
            physical = []
            for filename in db.files(db.table('t')):
                with Reader(db.path / filename, db.table('t')['columns']) as reader:
                    physical.extend([offset, row[2], row[0]] for offset, row in reader)
            packed = {filename: list(journal.reference(filename)) for filename in db.files(db.table('t'))}
            before = [disk_observe(db, statement) for statement in (TOP, ALIASED)]
        with closing(Database(path)) as db:
            db.query_cache_limit = 0
            reopened = [disk_observe(db, statement) for statement in (TOP, ALIASED)]
        assert [x['output'] for x in before] == [x['output'] for x in reopened]
        results.append({'input': label, 'physical_offset_primary_tag': physical,
                        'packed_segment_locations': packed,
                        'before': before, 'reopened': reopened})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run', 'matrix', 'disk'), default='prepare', nargs='?')
    args = parser.parse_args()
    print(json.dumps({'create': CREATE, 'insert': INSERT, 'probes': PROBES,
                      'read': READ, 'top': TOP}))
    if args.stage == 'run':
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(run(Path(temporary) / 'data')))
    elif args.stage == 'matrix':
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(matrix(Path(temporary))))
    elif args.stage == 'disk':
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(disk_migration(Path(temporary))))
