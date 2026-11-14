"""Immutable Python row segments, with a legacy JSONL reader.

The catalog remains the commit record. A binary segment contains a fixed
header, length-delimited typed rows, then (primary key, row offset) records.
The footer replaces the JSONL key sidecar and is spooled with bounded memory.
"""
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile

MAGIC = b'MVROWS1\0'
HEADER = struct.Struct('<8sIIQQ')
U64 = struct.Struct('<Q')
I64 = struct.Struct('<q')
LOCATION = struct.Struct('<qQ')
FOOTER_MEMORY = 256 * 1024


def _header(source, columns=None):
    block = source.read(HEADER.size)
    if len(block) != HEADER.size:
        raise OSError('truncated row segment header')
    magic, width, reserved, count, footer = HEADER.unpack(block)
    size = os.fstat(source.fileno()).st_size
    if (magic != MAGIC or reserved != 0 or (columns is not None and width != columns)
            or footer < HEADER.size or footer + count * LOCATION.size != size):
        raise OSError('invalid row segment header')
    return count, footer


def row_count(path):
    """Cheap count for cache admission and derived-index validation."""
    path = Path(path)
    if path.suffix == '.mvb':
        with path.open('rb') as source:
            return _header(source)[0]
    keys = Path(str(path) + '.keys')
    return keys.stat().st_size // I64.size if keys.exists() else None


def payload_size(path):
    """Lower bound for decoded data; exclude binary framing and footer."""
    path = Path(path)
    with path.open('rb') as source:
        count, footer = _header(source)
    return footer - HEADER.size - count * U64.size


def write(path, columns, rows):
    primary = next(i for i, column in enumerate(columns) if column['primary'])
    formats = [struct.Struct('<%df' % c['size']) if c['kind'] == 'VECTOR' else None
               for c in columns]
    # All spill I/O belongs to the database directory and its measured volume.
    with path.open('wb') as output, tempfile.SpooledTemporaryFile(
            max_size=FOOTER_MEMORY, dir=str(path.parent), mode='w+b') as locations:
        output.write(HEADER.pack(MAGIC, len(columns), 0, 0, HEADER.size))
        count = 0
        for row in rows:
            parts = []
            for column, fmt, value in zip(columns, formats, row):
                if column['kind'] == 'BIGINT':
                    parts.append(I64.pack(value))
                elif column['kind'] == 'VARCHAR':
                    encoded = value.encode('utf-8')
                    parts.extend((U64.pack(len(encoded)), encoded))
                else:
                    parts.append(fmt.pack(*value))
            payload = b''.join(parts)
            offset = output.tell()
            output.write(U64.pack(len(payload)))
            output.write(payload)
            locations.write(LOCATION.pack(row[primary], offset))
            count += 1
        footer = output.tell()
        locations.seek(0)
        shutil.copyfileobj(locations, output, length=64 * 1024)
        output.seek(0)
        output.write(HEADER.pack(MAGIC, len(columns), 0, count, footer))
        output.flush()
        os.fsync(output.fileno())


class Reader:
    def __init__(self, path, columns):
        self.source = Path(path).open('rb')
        self.columns = columns
        self.binary = Path(path).suffix == '.mvb'
        try:
            if self.binary:
                self.count, self.footer = _header(self.source, len(columns))
                self.formats = [struct.Struct('<%df' % c['size']) if c['kind'] == 'VECTOR'
                                else None for c in columns]
                self.minimum = sum(4*c['size'] if c['kind'] == 'VECTOR' else 8 for c in columns)
                self.maximum = self.minimum + sum(4*c['size'] for c in columns if c['kind'] == 'VARCHAR')
                if self.footer < HEADER.size + self.count * (U64.size + self.minimum):
                    raise OSError('row count exceeds segment data')
        except BaseException:
            self.source.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.source.close()

    def _row(self):
        source = self.source
        block = source.read(U64.size)
        if len(block) != U64.size:
            raise OSError('truncated row length')
        length = U64.unpack(block)[0]
        if not self.minimum <= length <= self.maximum or length > self.footer - source.tell():
            raise OSError('invalid row length')
        payload = source.read(length)
        if len(payload) != length:
            raise OSError('truncated row payload')
        result, position = [], 0
        try:
            for column, fmt in zip(self.columns, self.formats):
                if column['kind'] == 'BIGINT':
                    value = I64.unpack_from(payload, position)[0]
                    position += I64.size
                elif column['kind'] == 'VARCHAR':
                    size = U64.unpack_from(payload, position)[0]
                    position += U64.size
                    if size > length - position:
                        raise OSError('invalid string length')
                    value = payload[position:position+size].decode('utf-8')
                    if len(value) > column['size']:
                        raise OSError('string exceeds column width')
                    position += size
                else:
                    value = list(fmt.unpack_from(payload, position))
                    position += fmt.size
                    if not all(map(math.isfinite, value)):
                        raise OSError('nonfinite stored vector')
                result.append(value)
        except (struct.error, UnicodeError) as exc:
            raise OSError('invalid row payload') from exc
        if position != length:
            raise OSError('row payload has trailing bytes')
        return result

    def at(self, offset):
        if self.binary and not HEADER.size <= offset < self.footer:
            raise OSError('row offset outside segment')
        self.source.seek(offset)
        return self._row() if self.binary else json.loads(self.source.readline())

    def __iter__(self):
        source = self.source
        source.seek(HEADER.size if self.binary else 0)
        if self.binary:
            for _ in range(self.count):
                offset = source.tell()
                yield offset, self._row()
            if source.tell() != self.footer:
                raise OSError('row count does not match segment data')
        else:
            while True:
                offset = source.tell()
                line = source.readline()
                if not line:
                    break
                yield offset, json.loads(line)

    def keys(self, primary):
        if not self.binary:
            yield from (row[primary] for _, row in self)
            return
        self.source.seek(self.footer)
        remaining = self.count
        while remaining:
            count = min(remaining, 4096)
            chunk = self.source.read(count * LOCATION.size)
            if len(chunk) != count * LOCATION.size:
                raise OSError('truncated row footer')
            for key, offset in LOCATION.iter_unpack(chunk):
                if not HEADER.size <= offset < self.footer:
                    raise OSError('invalid footer row offset')
                yield key
            remaining -= count
