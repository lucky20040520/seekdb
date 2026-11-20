"""Compare SQL lexical boundaries across JSONL and the diagnostic adapter."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import queue
import threading

from minivector.engine import Database
from minivector.sql import execute


TEXT = "q;';--/*x*/😀"
SINGLE = "INSERT INTO t VALUES (2,'" + TEXT.replace("'", "''") + "');"
MULTI = "INSERT INTO t VALUES (3,'three'); INSERT INTO t VALUES (4,'four');"
READ = 'SELECT id,tag FROM t ORDER BY id;'
REQUESTS = [
    {'id': 'literal', 'op': 'execute', 'sql': SINGLE},
    {'id': 'multi', 'op': 'execute', 'sql': MULTI},
    {'id': 'read', 'op': 'execute', 'sql': READ},
    {'id': 'bye', 'op': 'shutdown'},
]


def run(root, protocol, duplicate=False, transaction=False):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(64));')
        execute(db, "INSERT INTO t VALUES (1,'seed');")
    multi = ("INSERT INTO t VALUES (3,'three'); INSERT INTO t VALUES (1,'duplicate');"
             if duplicate else MULTI)
    requests = ([{'id': 'multi', 'op': 'execute', 'sql': multi},
                 {'id': 'read', 'op': 'execute', 'sql': READ},
                 {'id': 'bye', 'op': 'shutdown'}] if duplicate else REQUESTS)
    wire = ('\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'
            if protocol == 'jsonl' else ('' if duplicate else SINGLE + '\n') + multi + '\n' + READ + '\n')
    if transaction:
        statements = ['BEGIN;', "INSERT INTO t VALUES (3,'three');",
                      "INSERT INTO t VALUES (1,'duplicate');", READ, 'ROLLBACK;', READ]
        requests = [{'id': i + 1, 'op': 'execute', 'sql': sql}
                    for i, sql in enumerate(statements)] + [{'id': 'bye', 'op': 'shutdown'}]
        wire = ('\n'.join(json.dumps(r, ensure_ascii=False) for r in requests) + '\n'
                if protocol == 'jsonl' else '\n'.join(statements) + '\n')
    command = [sys.executable, '-u', '-m', 'minivector', '--data-dir', str(root),
               '--protocol', protocol]
    child = subprocess.run(command, input=wire, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           encoding='utf-8', timeout=15,
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    assert child.returncode == 0, child.stderr
    with closing(Database(root)) as db:
        reopened = list(execute(db, READ)[1])
    return {'protocol': protocol, 'wire': wire, 'stdout': child.stdout,
            'stderr': child.stderr, 'exit_code': child.returncode, 'reopened_rows': reopened}


def fragmented(root):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(64));')
        execute(db, "INSERT INTO t VALUES (1,'seed');")
    process = subprocess.Popen([sys.executable, '-u', '-m', 'minivector', '--data-dir', str(root)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    events, lines = queue.Queue(), []
    errors = []

    def read_lines():
        for line in iter(process.stdout.readline, b''):
            events.put(line)
        events.put(None)

    def read_errors():
        errors.append(process.stderr.read())

    threads = [threading.Thread(target=reader, daemon=True) for reader in (read_lines, read_errors)]
    for thread in threads:
        thread.start()

    def response():
        line = events.get(timeout=5)
        assert line is not None, 'process exited before expected response'
        lines.append(line)
        return json.loads(line.decode('utf-8'))

    request = json.dumps(REQUESTS[0], ensure_ascii=False).encode('utf-8')
    cut = request.index('😀'.encode('utf-8')) + 1
    pieces = [request[:cut], request[cut:], b'\n']
    observations = []
    try:
        assert response() == {'type': 'ready'}
        for piece in pieces[:2]:
            process.stdin.write(piece)
            process.stdin.flush()
            try:
                premature = events.get(timeout=0.2)
            except queue.Empty:
                observations.append({'sent_bytes': len(piece), 'response_within_0_2s': False})
            else:
                raise AssertionError(('unexpected output before newline', premature))
        process.stdin.write(pieces[2])
        process.stdin.flush()
        inserted = response()
        assert inserted['id'] == 'literal' and inserted['ok']
        for item in REQUESTS[2:]:
            process.stdin.write((json.dumps(item) + '\n').encode('utf-8'))
            process.stdin.flush()
            response()
        process.stdin.close()
        exit_code = process.wait(timeout=5)
        assert events.get(timeout=5) is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    with closing(Database(root)) as db:
        reopened = list(execute(db, READ)[1])
    assert reopened == [[1, 'seed'], [2, TEXT]]
    return {'pieces_hex': [piece.hex() for piece in pieces], 'observations': observations,
            'stdout': b''.join(lines).decode('utf-8'), 'stderr': b''.join(errors).decode('utf-8'),
            'exit_code': exit_code, 'reopened_rows': reopened,
            'scope': 'two finite 0.2-second no-response windows; not proof over all timing schedules'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run', 'duplicate', 'transaction', 'fragmented'), default='prepare', nargs='?')
    args = parser.parse_args()
    print(json.dumps({'literal_text': TEXT, 'single': SINGLE, 'multi': MULTI,
                      'requests': REQUESTS}, ensure_ascii=True))
    if args.stage in ('run', 'duplicate', 'transaction'):
        with tempfile.TemporaryDirectory() as temporary:
            for protocol in ('jsonl', 'diagnostic'):
                print(json.dumps(run(Path(temporary) / protocol, protocol,
                                     duplicate=args.stage == 'duplicate',
                                     transaction=args.stage == 'transaction'), ensure_ascii=True))
    elif args.stage == 'fragmented':
        with tempfile.TemporaryDirectory() as temporary:
            print(json.dumps(fragmented(Path(temporary) / 'jsonl'), ensure_ascii=True))
