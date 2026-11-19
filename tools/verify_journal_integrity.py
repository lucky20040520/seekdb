"""Isolated byte-corruption diagnostics, outside ordinary process-crash guarantees."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from minivector import journal
from minivector.engine import Database
from minivector.sql import execute


def prepare(root):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY);')
        execute(db, 'INSERT INTO t VALUES (1);')
        first_end = db.journal.committed_end
        execute(db, 'INSERT INTO t VALUES (2);')
        second_end = db.journal.committed_end
        rows = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
    name = (root / 'CURRENT').read_text(encoding='ascii').strip()
    data = (root / name).read_bytes()
    frames, offset = [], len(journal.MAGIC)
    while offset < len(data):
        magic, kind, length, digest = journal.FRAME.unpack_from(data, offset)
        assert magic == journal.FRAME_MAGIC
        payload = offset + journal.FRAME.size
        end = payload + length + journal.SEAL.size
        assert data[payload + length:end] == journal.SEAL.pack(journal.SEAL_MAGIC, length)
        assert hashlib.sha256(bytes([kind]) + length.to_bytes(8, 'little') + data[payload:payload + length]).digest() == digest
        frames.append({'kind': kind, 'start': offset, 'payload': payload, 'length': length, 'end': end})
        offset = end
    assert offset == len(data) == second_end and rows == [[1], [2]]
    return {'name': name, 'first_end': first_end, 'second_end': second_end,
            'frames': frames, 'acknowledged_rows': rows,
            'scope': 'Windows local core API; manual post-close file changes, not a power-loss simulation'}


def trial(root, baseline, setup, case):
    shutil.copytree(str(baseline), str(root))
    path = root / setup['name']
    original = path.read_bytes()
    data = bytearray(original)
    commits = [frame for frame in setup['frames'] if frame['kind'] == journal.COMMIT]
    if case == 'partial-tail':
        data.extend(journal.FRAME_MAGIC[:5])
        change = {'offset': len(original), 'appended_hex': journal.FRAME_MAGIC[:5].hex()}
    else:
        target = commits[-1] if case == 'last-commit-corrupt' else commits[0]
        offset = target['payload']
        data[offset] ^= 1
        change = {'offset': offset, 'before_byte': original[offset], 'after_byte': data[offset],
                  'digest_updated': False, 'frame': target}
    path.write_bytes(data)
    result = {'case': case, 'change': change, 'size_before_open': len(data),
              'sha256_before_open': hashlib.sha256(data).hexdigest()}
    try:
        with closing(Database(root)) as db:
            result.update(opened=True, rows=list(execute(db, 'SELECT id FROM t ORDER BY id;')[1]),
                          committed_end=db.journal.committed_end)
    except Exception as exc:
        result.update(opened=False, error_type=type(exc).__name__, error=str(exc))
    result['size_after_open'] = path.stat().st_size
    result['sha256_after_open'] = hashlib.sha256(path.read_bytes()).hexdigest()
    if result['opened']:
        with closing(Database(root)) as db:
            result['second_open_rows'] = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
    return result


def data_only(root, baseline, setup):
    with closing(Database(baseline)) as db:
        execute(db, 'BEGIN;')
        execute(db, 'INSERT INTO t VALUES (3);')
        visible_inside = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
        db.journal.source.flush()
        shutil.copytree(str(baseline), str(root))
        # Closing the original rolls back; the copied directory retains the tail.
    path = root / setup['name']
    data = path.read_bytes()
    start = setup['second_end']
    magic, kind, length, digest = journal.FRAME.unpack_from(data, start)
    payload = start + journal.FRAME.size
    assert magic == journal.FRAME_MAGIC and kind == journal.DATA
    assert payload + length + journal.SEAL.size == len(data)
    assert data[payload + length:] == journal.SEAL.pack(journal.SEAL_MAGIC, length)
    assert hashlib.sha256(bytes([kind]) + length.to_bytes(8, 'little') + data[payload:payload + length]).digest() == digest
    result = {'case': 'valid-data-without-commit', 'transaction_rows_before_copy': visible_inside,
              'tail_kind': kind, 'tail_checksum_valid': True, 'tail_seal_valid': True,
              'tail_payload_bytes': length, 'size_before_open': len(data),
              'commit_sent': False, 'copied_before_original_rollback': True}
    with closing(Database(root)) as db:
        result['rows'] = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
        result['committed_end'] = db.journal.committed_end
    result['size_after_open'] = path.stat().st_size
    with closing(Database(root)) as db:
        result['second_open_rows'] = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
    return result


def middle_corrupt(root, baseline, setup):
    with closing(Database(baseline)) as db:
        execute(db, 'INSERT INTO t VALUES (3);')
        acknowledged = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
    shutil.copytree(str(baseline), str(root))
    path = root / setup['name']
    data = bytearray(path.read_bytes())
    target = next(frame for frame in setup['frames']
                  if frame['kind'] == journal.DATA and frame['start'] == setup['first_end'])
    position = setup['second_end']
    later = []
    while position < len(data):
        magic, kind, length, digest = journal.FRAME.unpack_from(data, position)
        payload = position + journal.FRAME.size
        end = payload + length + journal.SEAL.size
        assert magic == journal.FRAME_MAGIC
        assert data[payload + length:end] == journal.SEAL.pack(journal.SEAL_MAGIC, length)
        assert hashlib.sha256(bytes([kind]) + length.to_bytes(8, 'little') + data[payload:payload + length]).digest() == digest
        later.append({'kind': kind, 'start': position, 'end': end, 'checksum_valid': True})
        position = end
    assert [frame['kind'] for frame in later] == [journal.DATA, journal.COMMIT]
    data[target['payload']] ^= 1
    path.write_bytes(data)
    result = {'case': 'middle-data-corrupt', 'acknowledged_rows': acknowledged,
              'damaged_payload_offset': target['payload'], 'digest_updated': False,
              'later_frames': later, 'size_before_open': len(data)}
    with closing(Database(root)) as db:
        result['rows'] = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
        result['committed_end'] = db.journal.committed_end
    result['size_after_open'] = path.stat().st_size
    with closing(Database(root)) as db:
        result['second_open_rows'] = list(execute(db, 'SELECT id FROM t ORDER BY id;')[1])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'run', 'data-only', 'middle'), default='prepare', nargs='?')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        baseline = root / 'baseline'
        setup = prepare(baseline)
        print(json.dumps({'setup': setup}, ensure_ascii=False))
        if args.stage == 'run':
            for case in ('partial-tail', 'last-commit-corrupt', 'initial-commit-corrupt'):
                print(json.dumps(trial(root / case, baseline, setup, case), ensure_ascii=False))
        elif args.stage == 'data-only':
            print(json.dumps(data_only(root / 'copy', baseline, setup), ensure_ascii=False))
        elif args.stage == 'middle':
            print(json.dumps(middle_corrupt(root / 'copy', baseline, setup), ensure_ascii=False))
