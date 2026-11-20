"""Observe whole-request validation before SQL side effects via real JSONL."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from minivector.engine import Database
from minivector.sql import execute


LINES = [
    '{"id":"literal","op":"execute","sql":"INSERT INTO t VALUES (2,\'NaN\');"}',
    '{"id":"nan-extra","op":"execute","sql":"INSERT INTO t VALUES (3,\'not-run\');","ignored":NaN}',
    '{"id":"duplicate-sql","op":"execute","sql":"INSERT INTO t VALUES (4,\'first\');","sql":"INSERT INTO t VALUES (5,\'second\');"}',
    '{"id":"read","op":"execute","sql":"SELECT id,tag FROM t ORDER BY id;"}',
    '{"id":"bye","op":"shutdown"}',
]


def run(root, lines=None):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(32));')
        execute(db, "INSERT INTO t VALUES (1,'seed');")
    wire = '\n'.join(LINES if lines is None else lines) + '\n'
    child = subprocess.run([sys.executable, '-u', '-m', 'minivector', '--data-dir', str(root)],
                           input=wire, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           encoding='utf-8', timeout=15,
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    assert child.returncode == 0, child.stderr
    messages = [json.loads(line) for line in child.stdout.splitlines()]
    with closing(Database(root)) as db:
        rows = list(execute(db, 'SELECT id,tag FROM t ORDER BY id;')[1])
    return {'wire': wire, 'stdout': child.stdout, 'stderr': child.stderr,
            'exit_code': child.returncode, 'messages': messages, 'reopened_rows': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run'), default='prepare', nargs='?')
    args = parser.parse_args()
    print(json.dumps({'initial_rows': [[1, 'seed']], 'raw_lines': LINES,
                      'scope': 'duplicate-key requests deliberately use raw text, not dict serialization'}))
    if args.stage == 'run':
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(run(Path(temporary) / 'data')))
