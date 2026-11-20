"""Observe table/index definitions inside commit and rollback boundaries."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector import flat_index
from minivector.engine import Database, DBError
from minivector.sql import execute


STATEMENTS = ['BEGIN;', 'CREATE TABLE u(id BIGINT PRIMARY KEY);',
              'INSERT INTO u VALUES (7);', 'CREATE VECTOR INDEX vi ON t(v);']


def state(db):
    try:
        u = {'rows': list(execute(db, 'SELECT id FROM u ORDER BY id;')[1])}
    except DBError as error:
        u = {'code': error.code, 'message': str(error)}
    return {'visible_tables': sorted(db.visible), 'committed_tables': sorted(db.catalog),
            'visible_t_indexes': sorted(db.table('t')['indexes']),
            'committed_t_indexes': sorted(db.catalog['t']['indexes']),
            'u': u, 't_top1': list(execute(db,
                'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1]),
            'flat_files': sorted(path.name for path in db.path.glob('index-*.flat'))}


def trial(root, ending):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        execute(db, 'INSERT INTO t VALUES (1,[1,0]);')
        db.query_cache_limit = 0
        before = state(db)
        for sql in STATEMENTS:
            execute(db, sql)
        inside = state(db)
        execute(db, ending + ';')
        after = state(db)
    with closing(Database(root)) as db:
        db.query_cache_limit = 0
        reopened = state(db)
    return {'ending': ending, 'before': before, 'inside': inside,
            'after': after, 'reopened': reopened}


def reuse(root, ending):
    first = trial(root, ending)
    with closing(Database(root)) as db:
        db.query_cache_limit = 0
        before = state(db)
        actions = []
        with patch.object(flat_index, 'build', wraps=flat_index.build) as build:
            for sql in ('CREATE TABLE u(id BIGINT PRIMARY KEY);',
                        'CREATE VECTOR INDEX vi ON t(v);'):
                try:
                    execute(db, sql)
                    response = {'ok': True}
                except DBError as error:
                    response = {'ok': False, 'code': error.code, 'message': str(error)}
                actions.append({'sql': sql, 'response': response, 'state': state(db)})
            builds = build.call_count
        after = state(db)
    with closing(Database(root)) as db:
        db.query_cache_limit = 0
        reopened = state(db)
    assert reopened == after
    if ending == 'ROLLBACK':
        assert all(action['response']['ok'] for action in actions)
        assert reopened['u'] == {'rows': []} and builds == 1
    else:
        assert all(action['response'].get('code') == 'SCHEMA' for action in actions)
        assert before == after and reopened['u'] == {'rows': [[7]]} and builds == 0
    return {'ending': ending, 'initial_reopened': first['reopened'], 'before': before,
            'actions': actions, 'index_builds': builds, 'after': after, 'reopened': reopened}


def build_failure(root):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        execute(db, 'INSERT INTO t VALUES (1,[1,0]);')
        execute(db, 'INSERT INTO t VALUES (2,[2,0]);')
        db.query_cache_limit = 0
        execute(db, 'BEGIN;')
        execute(db, 'CREATE TABLE u(id BIGINT PRIMARY KEY);')
        execute(db, 'INSERT INTO u VALUES (7);')
        original = flat_index.build
        calls, completed = [], []

        def injected(*args, **kwargs):
            calls.append(str(args[1]))
            if len(calls) == 2:
                raise OSError('diagnostic before second index build')
            filename = original(*args, **kwargs)
            completed.append(filename)
            return filename

        with patch.object(flat_index, 'build', injected):
            try:
                execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            except OSError as error:
                failure = {'type': type(error).__name__, 'message': str(error)}
            else:
                raise AssertionError('fault injection not reached')
        after_failure = state(db)
        active = db.transaction is not None
        first_exists = [(db.path / filename).exists() for filename in completed]
        poisoned = db.journal.poisoned
        with patch.object(flat_index, 'build', wraps=flat_index.build) as retry:
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            retried = state(db)
            retry_builds = retry.call_count
        mapping = dict(db.table('t')['indexes']['vi']['segments'])
        execute(db, 'COMMIT;')
        committed = state(db)
    with closing(Database(root)) as db:
        db.query_cache_limit = 0
        reopened = state(db)
        reopened_mapping = dict(db.table('t')['indexes']['vi']['segments'])
    assert after_failure['u'] == {'rows': [[7]]}
    assert after_failure['visible_t_indexes'] == [] and after_failure['committed_tables'] == ['t']
    assert first_exists == [False] and active and not poisoned
    assert retry_builds == 2 and len(mapping) == 2 and reopened_mapping == mapping
    assert committed == reopened and reopened['u'] == {'rows': [[7]]}
    return {'fault_scope': 'wrapper raises before second build, not real disk error',
            'failure': failure, 'build_calls': calls, 'first_completed_files': completed,
            'first_files_exist_after_failure': first_exists, 'transaction_active': active,
            'journal_poisoned': poisoned, 'after_failure': after_failure,
            'retry_builds': retry_builds, 'retried': retried, 'committed': committed,
            'reopened': reopened, 'index_mapping': mapping}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run', 'reuse', 'build-failure'), default='prepare', nargs='?')
    args = parser.parse_args()
    print(json.dumps({'initial': 't={(1,[1,0])}, no u, no vector index',
                      'statements': STATEMENTS, 'endings': ['ROLLBACK', 'COMMIT']}))
    if args.stage == 'run':
        with tempfile.TemporaryDirectory() as temporary:
            for ending in ('ROLLBACK', 'COMMIT'):
                print(json.dumps(trial(Path(temporary) / ending, ending)))
    elif args.stage == 'reuse':
        with tempfile.TemporaryDirectory() as temporary:
            for ending in ('ROLLBACK', 'COMMIT'):
                print(json.dumps(reuse(Path(temporary) / ending, ending)))
    elif args.stage == 'build-failure':
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(build_failure(Path(temporary) / 'data')))
