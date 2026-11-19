"""Global DESC/tie ordering across actual external-sort runs."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from minivector import engine
from minivector.engine import Database
from minivector.sql import execute


PAIRS = [(9, 7), (4, 8), (2, 7), (8, 8), (1, 7), (3, 8)]


def observe(db, descending, limit):
    made = []
    original = engine.tempfile.NamedTemporaryFile
    original_sort, original_merge = db.external_sort, engine.heapq.merge
    input_state = {'rows': 0, 'bytes_since_spill': 0}
    merges = []

    def external(rows, key):
        def source():
            try:
                for row in rows:
                    input_state['rows'] += 1
                    input_state['bytes_since_spill'] += len(engine.encode(row).encode('utf-8'))
                    yield row
            finally:
                if hasattr(rows, 'close'):
                    rows.close()
        return original_sort(source(), key)

    def merge(*streams, **kwargs):
        merges.append(len(streams))
        return original_merge(*streams, **kwargs)

    class ObservedFile:
        def __init__(self, handle, record):
            self.handle, self.record = handle, record

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, text):
            row = json.loads(text)
            self.record['rows'].append(row[:2])
            start = self.handle.tell()
            written = self.handle.write(text)
            self.record['text_positions'].append([start, self.handle.tell()])
            return written

    def temporary(*args, **kwargs):
        handle = original(*args, **kwargs)
        if kwargs.get('prefix') != 'sort-':
            return handle
        record = {'path': handle.name, 'rows': [], 'text_positions': [],
                  'input_rows_seen': input_state['rows'],
                  'encoded_row_bytes_since_spill': input_state['bytes_since_spill']}
        input_state['bytes_since_spill'] = 0
        made.append(record)
        return ObservedFile(handle, record)

    sql = 'SELECT id,score FROM t ORDER BY score ' + ('DESC' if descending else 'ASC') + ' LIMIT %d;' % limit
    with patch.object(engine.tempfile, 'NamedTemporaryFile', temporary), \
            patch.object(engine.heapq, 'merge', merge), \
            patch.object(db, 'external_sort', side_effect=external) as external_calls:
        headers, output = execute(db, sql)
        rows = list(output)
    expected = [list(row) for row in sorted(PAIRS, key=lambda pair: (-pair[1] if descending else pair[1], pair[0]))[:limit]]
    assert rows == expected, (rows, expected)
    assert not list(db.path.glob('sort-*.jsonl'))
    origins = {pk: index + 1 for index, record in enumerate(made) for pk, _ in record['rows']}
    return {'sql': sql, 'columns': headers, 'rows': rows, 'truth': expected,
            'external_sort_calls': external_calls.call_count, 'merge_stream_counts': merges,
            'runs': [{key: value for key, value in record.items() if key != 'path'} for record in made],
            'output_origin_runs': [origins.get(row[0]) for row in rows],
            'all_created_files_removed': all(not Path(record['path']).exists() for record in made)}


def run(root, payload_length, reverse=False):
    pairs = list(reversed(PAIRS)) if reverse else PAIRS
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,score BIGINT,payload VARCHAR(700000));')
        db.insert('t', [[pk, score, 'x' * payload_length] for pk, score in pairs])
        cases = [(True, 4), (False, 6)] + ([(True, 6)] if reverse else [])
        outputs = [observe(db, descending, limit) for descending, limit in cases]
    return {'payload_ascii_length': payload_length, 'input_order': pairs, 'queries': outputs}


def fan_in(db):
    original_temp, original_open, original_merge = engine.tempfile.NamedTemporaryFile, Path.open, engine.heapq.merge
    files, by_name, reads, writes, merges = [], {}, set(), set(), []
    peaks = {'sort_readers': 0, 'sort_writers': 0, 'sort_readers_plus_writers': 0}
    input_count = [0]

    def peak():
        peaks['sort_readers'] = max(peaks['sort_readers'], len(reads))
        peaks['sort_writers'] = max(peaks['sort_writers'], len(writes))
        peaks['sort_readers_plus_writers'] = max(peaks['sort_readers_plus_writers'], len(reads) + len(writes))

    class Writer:
        def __init__(self, handle, record):
            self.handle, self.record = handle, record

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            try:
                return self.handle.__exit__(*args)
            finally:
                writes.discard(self.record['id'])

        def close(self):
            try:
                self.handle.close()
            finally:
                writes.discard(self.record['id'])

        def write(self, text):
            first = text.index(',')
            second = text.index(',', first + 1)
            pair = [int(text[1:first]), int(text[first + 1:second])]
            count = self.handle.write(text)
            self.record['written'].append(pair)
            return count

    class Reader:
        def __init__(self, handle, record):
            self.handle, self.record = handle, record

        def __iter__(self):
            return self

        def __next__(self):
            line = next(self.handle)
            self.record['read_keys'].append(int(line[1:line.index(',')]))
            return line

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            try:
                self.handle.close()
            finally:
                reads.discard(self.record['id'])

    def temporary(*args, **kwargs):
        handle = original_temp(*args, **kwargs)
        if kwargs.get('prefix') != 'sort-':
            return handle
        record = {'id': len(files) + 1, 'name': handle.name, 'written': [], 'read_keys': []}
        files.append(record)
        by_name[Path(handle.name).name] = record
        writes.add(record['id'])
        peak()
        return Writer(handle, record)

    def opened(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if not path.name.startswith('sort-'):
            return handle
        record = by_name[path.name]
        reads.add(record['id'])
        peak()
        return Reader(handle, record)

    def merge(*streams, **kwargs):
        merges.append({'inputs': len(streams), 'reader_ids': sorted(reads), 'writer_ids': sorted(writes)})
        return original_merge(*streams, **kwargs)

    payload = 'x' * 2100000

    def source():
        for primary in range(33, 0, -1):
            input_count[0] += 1
            yield [primary, primary % 2, payload]

    output = []
    first_result_input_count = None
    with patch.object(engine.tempfile, 'NamedTemporaryFile', temporary), \
            patch.object(Path, 'open', opened), patch.object(engine.heapq, 'merge', merge):
        stream = db.external_sort(source(), key=lambda row: (-row[1], row[0]))
        try:
            for row in stream:
                if first_result_input_count is None:
                    first_result_input_count = input_count[0]
                output.append(row[:2])
                if len(output) == 5:
                    break
        finally:
            stream.close()
    assert [row[0] for row in output] == [1, 3, 5, 7, 9]
    assert [item['inputs'] for item in merges] == [32, 1, 2]
    assert first_result_input_count == 33 and len(files) == 35
    assert peaks['sort_readers'] == 32 and peaks['sort_readers_plus_writers'] == 33
    assert not reads and not writes and not list(db.path.glob('sort-*.jsonl'))
    assert all(not Path(item['name']).exists() for item in files)
    return {'scope': 'direct external_sort core API, no SQL insertion or query parsing',
            'input_rows_when_first_result': first_result_input_count, 'payload_ascii_length': len(payload),
            'rows': output, 'merge_calls': merges, 'created_files': len(files), 'peaks': peaks,
            'live_sort_readers_after_close': len(reads), 'live_sort_writers_after_close': len(writes),
            'files': [{key: value for key, value in item.items() if key != 'name'} for item in files],
            'all_created_files_removed': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run', 'reverse', 'fan-in'), default='prepare', nargs='?')
    args = parser.parse_args()
    print(json.dumps({'input_order': PAIRS, 'payload_lengths': [16, 600000],
                      'declared_varchar_width': 700000, 'sort_spill_bytes': 2097152,
                      'queries': ['ORDER BY score DESC LIMIT 4', 'ORDER BY score ASC LIMIT 6'],
                      'scope': 'diagnostic write observer, no performance claim'}))
    if args.stage in ('run', 'reverse'):
        with tempfile.TemporaryDirectory() as temporary:
            for length in (16, 600000):
                print(json.dumps(run(Path(temporary) / str(length), length, reverse=args.stage == 'reverse')))
    elif args.stage == 'fan-in':
        with tempfile.TemporaryDirectory() as temporary:
            with closing(Database(Path(temporary) / 'data')) as db:
                print(json.dumps(fan_in(db)))
