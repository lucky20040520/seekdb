"""Append-only row segments and atomic catalog commits in one durable file.

Only a sealed, checksummed catalog record publishes preceding data records.
The CURRENT pointer changes only at creation/checkpoint, never per commit.
Readers retain bounded views of immutable payloads; rows need not fit in RAM.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import uuid

MAGIC = b'MVLOG01\0'
FRAME = struct.Struct('<8sBQ32s')
SEAL = struct.Struct('<8sQ')
FRAME_MAGIC, SEAL_MAGIC = b'MVFRAME1', b'MVSEAL1\0'
DATA, COMMIT = 1, 2
PACKED = re.compile(r'packed-([0-9a-f]{32})-([0-9a-f]{1,16})-([0-9a-f]{1,16})\.mvb\Z')
LOG = re.compile(r'journal-[0-9a-f]{32}\.mvj\Z')
CHECKPOINT_MIN = 8 * 1024 * 1024
CHECKPOINT_GARBAGE = 4 * 1024 * 1024


def reference(name):
    match = PACKED.fullmatch(name)
    return (match[1], int(match[2], 16), int(match[3], 16)) if match else None


class SegmentView:
    def __init__(self, path, offset, length):
        self.source = path.open('rb')
        self.offset, self.length = offset, length
        if offset < len(MAGIC)+FRAME.size or offset+length > os.fstat(self.source.fileno()).st_size:
            self.source.close()
            raise OSError('row segment outside journal')
        self.source.seek(offset)

    def read(self, count=-1):
        remaining = self.length-self.tell()
        return self.source.read(remaining if count < 0 else min(count, remaining))

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else (self.tell() if whence == 1 else self.length)+offset
        if not 0 <= position <= self.length:
            raise OSError('seek outside row segment')
        self.source.seek(self.offset+position)
        return position

    def tell(self):
        return self.source.tell()-self.offset

    def fileno(self):
        return self.source.fileno()

    def close(self):
        self.source.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_segment(path):
    path = Path(path)
    location = reference(path.name)
    if location is None:
        return path.open('rb')
    token, offset, length = location
    return SegmentView(path.parent/('journal-'+token+'.mvj'), offset, length)


def files(table):
    return table.get('files', [table['file']] if table['file'] else [])


def changes(previous, catalog):
    updates = {}
    for name, table in catalog.items():
        old = previous.get(name)
        if old == table:
            continue
        old_files = [] if old is None else files(old)
        new_files = files(table)
        if (old is not None and old['columns'] == table['columns'] and old['indexes'] == table['indexes']
                and len(new_files) > len(old_files) and new_files[:len(old_files)] == old_files):
            updates[name] = {'append': new_files[len(old_files):]}
        else:
            updates[name] = {'replace': table}
    return {'tables': updates, 'drop': [name for name in previous if name not in catalog]}


def apply_changes(catalog, delta):
    result = dict(catalog)
    for name in delta['drop']:
        del result[name]
    for name, update in delta['tables'].items():
        if 'replace' in update:
            result[name] = update['replace']
        else:
            table = dict(result[name])
            table['files'] = list(files(table))+update['append']
            table['file'] = None
            result[name] = table
    return result


class Journal:
    @property
    def source(self):
        if self._source is None:
            self._source = (self.directory/self.name).open('r+b')
        return self._source

    @source.setter
    def source(self, value):
        self._source = value

    def idle(self):
        if self._source is not None:
            self._source.close()
            self._source = None

    def __init__(self, directory, legacy, sync_directory):
        self.directory, self.sync_directory = Path(directory), sync_directory
        self.poisoned, self.closed = False, False
        pointer = self.directory/'CURRENT'
        if pointer.exists():
            if pointer.stat().st_size > 80:
                raise OSError('invalid journal pointer size')
            with pointer.open('r', encoding='ascii') as source:
                name = source.read(80).strip()
            if not LOG.fullmatch(name):
                raise OSError('invalid journal pointer')
            self.name, self.token = name, name[8:-4]
            self.source = (self.directory/name).open('r+b')
            try:
                self.catalog, self.committed_end = self.recover()
                self.discard_tail()
            except BaseException:
                self.idle()
                raise
        else:
            self._create()
            try:
                self.commit(legacy)
                self._publish_pointer()
            except BaseException:
                self.idle()
                raise

    def _create(self):
        self.token = uuid.uuid4().hex
        self.name = 'journal-'+self.token+'.mvj'
        self.source = (self.directory/self.name).open('x+b')
        self.source.write(MAGIC)
        self.catalog, self.committed_end = {}, len(MAGIC)

    def _publish_pointer(self):
        temporary = self.directory/('journal-pointer-'+uuid.uuid4().hex+'.tmp')
        with temporary.open('w', encoding='ascii', newline='\n') as output:
            output.write(self.name+'\n')
            output.flush()
            os.fsync(output.fileno())
        # The new journal name must survive before CURRENT can reference it.
        self.sync_directory(self.directory)
        os.replace(str(temporary), str(self.directory/'CURRENT'))
        self.sync_directory(self.directory)

    def recover(self):
        source = self.source
        if source.read(len(MAGIC)) != MAGIC:
            raise OSError('invalid journal header')
        size = os.fstat(source.fileno()).st_size
        catalog, committed = None, len(MAGIC)
        while source.tell() < size:
            header = source.read(FRAME.size)
            if len(header) != FRAME.size:
                break
            magic, kind, length, expected = FRAME.unpack(header)
            if magic != FRAME_MAGIC or kind not in (DATA, COMMIT) or length > size-source.tell()-SEAL.size:
                break
            payload = source.tell()
            digest = hashlib.sha256(bytes([kind])+struct.pack('<Q', length))
            remaining = length
            while remaining:
                chunk = source.read(min(remaining, 64*1024))
                if not chunk:
                    raise OSError('short journal read')
                digest.update(chunk)
                remaining -= len(chunk)
            seal = source.read(SEAL.size)
            if seal != SEAL.pack(SEAL_MAGIC, length) or digest.digest() != expected:
                break
            end = source.tell()
            if kind == COMMIT:
                source.seek(payload)
                try:
                    delta = json.loads(source.read(length))
                    catalog = apply_changes({} if catalog is None else catalog, delta)
                except (ValueError, TypeError, KeyError) as exc:
                    raise OSError('invalid journal catalog') from exc
                source.seek(end)
                committed = end
        if catalog is None:
            raise OSError('journal has no complete initial commit')
        return catalog, committed

    def savepoint(self):
        if self.closed or self.poisoned:
            raise OSError('journal requires restart')
        return self.source.seek(0, 2)

    def rewind(self, position):
        if position < self.committed_end:
            raise OSError('cannot truncate a committed record')
        self.source.seek(position)
        self.source.truncate()
        self.source.flush()

    def discard_tail(self):
        try:
            if not self.closed and not self.poisoned and self.source.seek(0, 2) != self.committed_end:
                self.rewind(self.committed_end)
        finally:
            self.idle()

    def _append(self, kind, writer):
        start = self.savepoint()
        source = self.source
        try:
            source.write(b'\0'*FRAME.size)
            payload = source.tell()
            writer(source)
            end = source.tell()
            length = end-payload
            digest = hashlib.sha256(bytes([kind])+struct.pack('<Q', length))
            source.seek(payload)
            remaining = length
            while remaining:
                chunk = source.read(min(remaining, 64*1024))
                if not chunk:
                    raise OSError('short journal payload')
                digest.update(chunk)
                remaining -= len(chunk)
            source.write(SEAL.pack(SEAL_MAGIC, length))
            source.seek(start)
            source.write(FRAME.pack(FRAME_MAGIC, kind, length, digest.digest()))
            source.seek(end+SEAL.size)
            source.flush()  # transaction reads use independent bounded readers
            return payload, length
        except BaseException:
            try:
                self.rewind(start)
            except BaseException:
                self.poisoned = True
            raise

    def append_segment(self, columns, rows):
        from .segments import write_stream
        try:
            offset, length = self._append(DATA, lambda output: write_stream(output, columns, rows, self.directory))
            return 'packed-%s-%x-%x.mvb' % (self.token, offset, length)
        finally:
            self.idle()

    def commit(self, catalog):
        payload = json.dumps(changes(self.catalog, catalog), ensure_ascii=False,
                             allow_nan=False, separators=(',', ':')).encode('utf-8')
        try:
            self._append(COMMIT, lambda output: output.write(payload))
            os.fsync(self.source.fileno())
            self.catalog, self.committed_end = catalog, self.source.tell()
        except BaseException:
            # A failed barrier may have persisted the commit. Keep every file
            # and refuse further writes until recovery decides the outcome.
            self.poisoned = True
            raise
        finally:
            self.idle()

    def checkpoint(self, catalog):
        size = self.source.seek(0, 2)
        self.idle()
        if size < CHECKPOINT_MIN:
            return catalog
        names = {name for table in catalog.values() for name in files(table)}
        live = sum(reference(name)[2]+FRAME.size+SEAL.size for name in names if reference(name))
        if size-live < max(CHECKPOINT_GARBAGE, live//2):
            return catalog
        target = object.__new__(Journal)
        target.directory, target.sync_directory = self.directory, self.sync_directory
        target.poisoned, target.closed = False, False
        target._create()
        try:
            mapping = {}
            for name in sorted(names):
                if reference(name) is None:
                    continue
                with open_segment(self.directory/name) as source:
                    offset, length = target._append(DATA, lambda output: shutil.copyfileobj(source, output, 64*1024))
                mapping[name] = 'packed-%s-%x-%x.mvb' % (target.token, offset, length)
            updated = copy.deepcopy(catalog)
            for table in updated.values():
                if 'files' in table:
                    table['files'] = [mapping.get(name, name) for name in table['files']]
                elif table['file']:
                    table['file'] = mapping.get(table['file'], table['file'])
                for descriptor in table['indexes'].values():
                    if 'segments' in descriptor:
                        descriptor['segments'] = {mapping.get(name, name): index
                                                  for name, index in descriptor['segments'].items()}
            target.commit(updated)
            target._publish_pointer()
        except BaseException:
            target.idle()
            self.poisoned = True  # pointer replacement may already be visible
            raise
        self.name, self.token = target.name, target.token
        self.catalog, self.committed_end = target.catalog, target.committed_end
        return updated

    def collect(self, catalog):
        if self.poisoned or self.closed:
            return
        live = {self.name}
        for table in catalog.values():
            for name in files(table):
                location = reference(name)
                if location:
                    live.add('journal-'+location[0]+'.mvj')
        for path in self.directory.glob('journal-*.mvj'):
            if LOG.fullmatch(path.name) and path.name not in live:
                path.unlink()
        for path in self.directory.glob('journal-pointer-*.tmp'):
            path.unlink()

    def close(self):
        if not self.closed:
            self.discard_tail()
            self.idle()
            self.closed = True
