"""Temporary, WAL-rebuildable page store with a bounded page cache."""
from collections import OrderedDict
import struct
import tempfile
import hashlib

PAGE_SIZE = 4096
_SIZE = struct.Struct("<I")
_INT = struct.Struct("<q")


class PageData:
    def __init__(self, directory, cache_pages=32):
        if cache_pages < 1:
            raise ValueError("cache_pages must be positive")
        # The WAL remains authoritative. Never open or truncate an existing
        # user data file while rebuilding the derived page store.
        self.file = tempfile.TemporaryFile(dir=directory, mode="w+b")
        self.cache = OrderedDict()
        self.cache_pages = cache_pages
        self.end = 0

    def append(self, schema, row):
        chunks = []
        for name, (kind, size) in schema["columns"].items():
            value = row[name]
            if kind == "BIGINT":
                chunks.append(_INT.pack(value))
            elif kind == "VARCHAR":
                data = value.encode("utf-8")
                chunks.extend((_SIZE.pack(len(data)), data))
            else:
                chunks.append(struct.pack("<" + str(size) + "f", *value))
        data = b"".join(chunks)
        offset = self.end
        self.file.seek(offset)
        self.file.write(_SIZE.pack(len(data)))
        self.file.write(data)
        self.end = offset + _SIZE.size + len(data)
        for page in range(offset // PAGE_SIZE, (self.end - 1) // PAGE_SIZE + 1):
            self.cache.pop(page, None)
        return offset

    def _read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.end:
            raise ValueError("invalid data offset")
        result = bytearray()
        while length:
            page, within = divmod(offset, PAGE_SIZE)
            data = self.cache.get(page)
            if data is None:
                self.file.seek(page * PAGE_SIZE)
                data = self.file.read(min(PAGE_SIZE, self.end - page * PAGE_SIZE))
                self.cache[page] = data
                while len(self.cache) > self.cache_pages:
                    self.cache.popitem(last=False)
            else:
                self.cache.move_to_end(page)
            take = min(length, len(data) - within)
            if take <= 0:
                raise ValueError("truncated data page")
            result.extend(data[within:within + take])
            offset += take
            length -= take
        return bytes(result)

    def read(self, schema, offset):
        length = _SIZE.unpack(self._read(offset, _SIZE.size))[0]
        data = self._read(offset + _SIZE.size, length)
        pos, row = 0, {}
        for name, (kind, size) in schema["columns"].items():
            if kind == "BIGINT":
                row[name] = _INT.unpack_from(data, pos)[0]
                pos += _INT.size
            elif kind == "VARCHAR":
                length = _SIZE.unpack_from(data, pos)[0]
                pos += _SIZE.size
                row[name] = data[pos:pos + length].decode("utf-8")
                pos += length
            else:
                row[name] = list(struct.unpack_from("<" + str(size) + "f", data, pos))
                pos += size * 4
        if pos != len(data):
            raise ValueError("invalid row data")
        return row

    def close(self):
        self.file.close()

    def export(self, target):
        self.file.flush()
        self.file.seek(0)
        digest = hashlib.sha256()
        while True:
            chunk = self.file.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
            digest.update(chunk)
        return digest.hexdigest()

    def load(self, source, expected_hash):
        digest = hashlib.sha256()
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            self.file.write(chunk)
            self.end += len(chunk)
            digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise ValueError("checkpoint page hash mismatch")
