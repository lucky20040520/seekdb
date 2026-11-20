"""Observe checkpoint reference remapping without changing logical table contents."""
from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import queue
from unittest.mock import patch

from minivector import flat_index, journal
from minivector.engine import Database
from minivector.segments import Reader
from minivector.sql import execute
from tools.verify_commit_cut import pump


def snapshot(db):
    table = db.table('t')
    reference, = db.files(table)
    indexes = dict(table['indexes']['vi']['segments'])
    index_file, = indexes.values()
    data = (db.path / index_file).read_bytes()
    with journal.open_segment(db.path / reference) as source:
        payload = source.read()
    with Reader(db.path / reference, table['columns']) as source:
        row_offsets = [[offset, row[0]] for offset, row in source]
    locations = [[values[0], values[1]]
                 for values in struct.Struct('<qQ2f').iter_unpack(data[flat_index.HEADER.size:])]
    with patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk:
        top = list(execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1])
    assert disk.call_count == 1 and top == [[1, 1.0]]
    return {'CURRENT': (db.path / 'CURRENT').read_text(encoding='ascii').strip(),
            'table_reference': reference, 'packed_location': list(journal.reference(reference)),
            'index_mapping': indexes, 'index_sha256': hashlib.sha256(data).hexdigest(),
            'payload_sha256': hashlib.sha256(payload).hexdigest(),
            'reader_offsets_and_keys': row_offsets, 'index_keys_and_offsets': locations,
            'rows': list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1]), 'top1': top}


def worker(root, cut):
    with closing(Database(root)) as db:
        original = journal.Journal._publish_pointer

        def pause(target):
            print(json.dumps({'cut': cut, 'old': db.journal.name, 'new': target.name,
                              'CURRENT': (db.path / 'CURRENT').read_text(encoding='ascii').strip(),
                              'old_exists': (db.path / db.journal.name).exists(),
                              'new_exists': (db.path / target.name).exists()}), flush=True)
            sys.stdin.buffer.read(1)
            raise RuntimeError('diagnostic barrier was released without termination')

        def publish(target):
            if cut == 'before-pointer':
                pause(target)
            original(target)
            if cut == 'after-pointer':
                pause(target)

        with patch.object(journal.Journal, '_publish_pointer', publish), \
                patch.object(journal, 'CHECKPOINT_MIN', 0), patch.object(journal, 'CHECKPOINT_GARBAGE', 0):
            db.collect()
        raise AssertionError('checkpoint did not reach barrier')


def crash_trial(root, cut):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        execute(db, 'INSERT INTO t VALUES (1,[1,0]),(2,[2,0]);')
        execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
        db.query_cache_limit = 0
        before = snapshot(db)
    process = subprocess.Popen([sys.executable, '-u', '-m', 'tools.verify_checkpoint_references',
                                '--worker', cut, '--data-dir', str(root)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    output, errors = queue.Queue(), queue.Queue()
    threads = [threading.Thread(target=pump, args=(stream, events), daemon=True)
               for stream, events in ((process.stdout, output), (process.stderr, errors))]
    for thread in threads:
        thread.start()
    try:
        line = output.get(timeout=10)
        assert line is not None, 'checkpoint worker exited before barrier'
        marker = json.loads(line)
        assert marker['cut'] == cut and marker['old_exists'] and marker['new_exists']
        selected = marker['old'] if cut == 'before-pointer' else marker['new']
        assert marker['CURRENT'] == selected
        process.kill()
        code = process.wait(timeout=10)
        assert code != 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    with patch.object(flat_index, 'build', wraps=flat_index.build) as rebuild:
        with closing(Database(root)) as db:
            db.query_cache_limit = 0
            recovered = snapshot(db)
            assert recovered['CURRENT'] == selected
            assert recovered['rows'] == before['rows'] and recovered['top1'] == before['top1']
            assert recovered['index_sha256'] == before['index_sha256']
            assert set(recovered['index_mapping']) == {recovered['table_reference']}
            remaining = sorted(path.name for path in db.path.glob('journal-*.mvj'))
            assert remaining == [selected]
    assert rebuild.call_count == 0
    return {'marker': marker, 'killed_exit': code, 'selected_on_recovery': selected,
            'remaining_journals': remaining, 'recovered': recovered, 'rebuild_calls': 0}


def commit_control(root):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        execute(db, 'INSERT INTO t VALUES (1,[1,0]),(2,[2,0]);')
        execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
        db.query_cache_limit = 0
        before = (db.path / 'CURRENT').read_text(encoding='ascii').strip()
        before_end = db.journal.committed_end
        execute(db, 'BEGIN;')
        assert execute(db, 'INSERT INTO t VALUES (3,[0,0]);') == 1
        events = []
        original_append, original_sync = db.journal._append, os.fsync

        def append(kind, writer):
            result = original_append(kind, writer)
            if kind == journal.COMMIT:
                events.append(['COMMIT_frame_complete', db.journal.committed_end])
            return result

        def sync(fd):
            source = db.journal._source
            is_journal = source is not None and source.fileno() == fd
            result = original_sync(fd)
            events.append(['journal_fsync_returned' if is_journal else 'other_fsync_returned',
                           db.journal.committed_end])
            return result

        with patch.object(db.journal, '_append', append), patch.object(os, 'fsync', sync):
            execute(db, 'COMMIT;')
        after = (db.path / 'CURRENT').read_text(encoding='ascii').strip()
        after_end = db.journal.committed_end
        events.append(['COMMIT_call_returned', after_end])
        assert [event[0] for event in events if event[0] != 'other_fsync_returned'] == [
            'COMMIT_frame_complete', 'journal_fsync_returned', 'COMMIT_call_returned']
        assert all(event[1] == before_end for event in events[:-1])
        assert before == after and after_end > before_end
        table = db.table('t')
        assert set(table['indexes']['vi']['segments']) == set(db.files(table))
        rows = list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1])
        with patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk:
            top = list(execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1])
        assert disk.call_count == 1 and top == [[3, 0.0]]
    with closing(Database(root)) as db:
        db.query_cache_limit = 0
        assert (db.path / 'CURRENT').read_text(encoding='ascii').strip() == after
        assert list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1]) == rows
        assert list(execute(db, 'SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 1;')[1]) == top
    return {'CURRENT_before': before, 'CURRENT_after': after, 'committed_end_before': before_end,
            'committed_end_after': after_end, 'commit_events': events,
            'rows': rows, 'top1': top, 'reopened_matches': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('normal', 'crashes', 'commit'), default='normal', nargs='?')
    parser.add_argument('--worker', choices=('before-pointer', 'after-pointer'))
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    if args.worker:
        worker(args.data_dir, args.worker)
        raise SystemExit(0)
    if args.stage == 'commit':
        with tempfile.TemporaryDirectory() as root:
            print(json.dumps(commit_control(root)))
        raise SystemExit(0)
    if args.stage == 'crashes':
        with tempfile.TemporaryDirectory() as root:
            for cut in ('before-pointer', 'after-pointer'):
                print(json.dumps(crash_trial(Path(root) / cut, cut)))
        raise SystemExit(0)
    with tempfile.TemporaryDirectory() as root:
        with closing(Database(root)) as db:
            execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
            execute(db, 'INSERT INTO t VALUES (1,[1,0]),(2,[2,0]);')
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
            db.query_cache_limit = 0
            before = snapshot(db)
            with patch.object(flat_index, 'build', wraps=flat_index.build) as build:
                # Diagnostic thresholds only. The production checkpoint copies,
                # publishes and collects the same way as at its normal size.
                with patch.object(journal, 'CHECKPOINT_MIN', 0), patch.object(journal, 'CHECKPOINT_GARBAGE', 0):
                    db.collect()
                after = snapshot(db)
            assert before['CURRENT'] != after['CURRENT']
            assert before['table_reference'] != after['table_reference']
            assert set(after['index_mapping']) == {after['table_reference']}
            assert list(before['index_mapping'].values()) == list(after['index_mapping'].values())
            for key in ('index_sha256', 'payload_sha256', 'reader_offsets_and_keys',
                        'index_keys_and_offsets', 'rows', 'top1'):
                assert before[key] == after[key], key
            assert not (db.path / before['CURRENT']).exists() and build.call_count == 0
        with patch.object(flat_index, 'build', wraps=flat_index.build) as rebuild:
            with closing(Database(root)) as db:
                db.query_cache_limit = 0
                reopened = snapshot(db)
                assert reopened == after
        assert rebuild.call_count == 0
        print(json.dumps({'before': before, 'after': after, 'old_journal_removed': True,
                          'checkpoint_index_build_calls': 0, 'reopen_index_build_calls': 0,
                          'reopened_exact_match': True}))
