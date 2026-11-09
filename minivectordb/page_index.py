"""Disk-backed B+ tree mapping signed 64-bit keys to WAL offsets.

The index is derived data: the database rebuilds it from the committed WAL on
open. It is not a durability boundary and must never be used without recovery.
"""
from bisect import bisect_left, bisect_right
from collections import OrderedDict
import struct


PAGE_SIZE = 4096
MAX_KEYS = 63
TOMBSTONE = (1 << 64) - 1
_HEADER = struct.Struct("<4sQQ")
_NODE = struct.Struct("<BHQ")  # type, count, next leaf (zero for internal)
_PAIR = struct.Struct("<qQ")
_U64 = struct.Struct("<Q")


class PageIndex:
    def __init__(self, path=None, cache_pages=32, create=False, fileobj=None):
        if cache_pages < 1:
            raise ValueError("cache_pages must be positive")
        self.cache_limit = cache_pages
        self.cache = OrderedDict()
        self.file = fileobj if fileobj is not None else open(path, "w+b" if create else "r+b")
        if create:
            self.root, self.count = 1, 1
            self._write_header()
            self._write_node(1, (True, [], [], 0))
        else:
            self.file.seek(0)
            magic, self.root, self.count = _HEADER.unpack(self.file.read(_HEADER.size))
            if magic != b"MVI1" or not 1 <= self.root <= self.count:
                raise ValueError("invalid index header")

    def _write_header(self):
        self.file.seek(0)
        self.file.write(_HEADER.pack(b"MVI1", self.root, self.count).ljust(PAGE_SIZE, b"\0"))

    def _cache(self, page, data):
        self.cache[page] = data
        self.cache.move_to_end(page)
        while len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)

    def _read_node(self, page):
        if page in self.cache:
            self.cache.move_to_end(page)
            data = self.cache[page]
        else:
            self.file.seek(page * PAGE_SIZE)
            data = self.file.read(PAGE_SIZE)
            if len(data) != PAGE_SIZE:
                raise ValueError("truncated index page")
            self._cache(page, data)
        typ, count, next_leaf = _NODE.unpack_from(data)
        if typ not in (1, 2) or count > MAX_KEYS:
            raise ValueError("invalid index page")
        keys, values = [], []
        pos = _NODE.size
        if typ == 2:
            values.append(_U64.unpack_from(data, pos)[0])
            pos += _U64.size
        for _ in range(count):
            key, value = _PAIR.unpack_from(data, pos)
            keys.append(key)
            values.append(value)
            pos += _PAIR.size
        return typ == 1, keys, values, next_leaf

    def _write_node(self, page, node):
        leaf, keys, values, next_leaf = node
        if len(keys) > MAX_KEYS or len(values) != len(keys) + (0 if leaf else 1):
            raise ValueError("invalid index node")
        data = bytearray(PAGE_SIZE)
        _NODE.pack_into(data, 0, 1 if leaf else 2, len(keys), next_leaf if leaf else 0)
        pos = _NODE.size
        if not leaf:
            _U64.pack_into(data, pos, values[0])
            pos += _U64.size
        for key, value in zip(keys, values if leaf else values[1:]):
            _PAIR.pack_into(data, pos, key, value)
            pos += _PAIR.size
        self.file.seek(page * PAGE_SIZE)
        self.file.write(data)
        self._cache(page, bytes(data))

    def _allocate(self, node):
        self.count += 1
        self._write_node(self.count, node)
        self._write_header()
        return self.count

    def get(self, key):
        page = self.root
        while True:
            leaf, keys, values, _ = self._read_node(page)
            if leaf:
                i = bisect_left(keys, key)
                return None if i == len(keys) or keys[i] != key or values[i] == TOMBSTONE else values[i]
            page = values[bisect_right(keys, key)]

    def put(self, key, value):
        if not -(1 << 63) <= key < (1 << 63) or not 0 <= value <= TOMBSTONE:
            raise ValueError("index key or value out of range")
        path = []
        page = self.root
        while True:
            leaf, keys, values, next_leaf = self._read_node(page)
            if leaf:
                break
            child = bisect_right(keys, key)
            path.append((page, child))
            page = values[child]
        i = bisect_left(keys, key)
        if i < len(keys) and keys[i] == key:
            values[i] = value
            self._write_node(page, (True, keys, values, next_leaf))
            return
        keys.insert(i, key)
        values.insert(i, value)
        if len(keys) <= MAX_KEYS:
            self._write_node(page, (True, keys, values, next_leaf))
            return
        middle = len(keys) // 2
        right = self._allocate((True, keys[middle:], values[middle:], next_leaf))
        self._write_node(page, (True, keys[:middle], values[:middle], right))
        separator = keys[middle]
        while path:
            parent, child = path.pop()
            _, parent_keys, children, _ = self._read_node(parent)
            parent_keys.insert(child, separator)
            children.insert(child + 1, right)
            if len(parent_keys) <= MAX_KEYS:
                self._write_node(parent, (False, parent_keys, children, 0))
                return
            middle = len(parent_keys) // 2
            separator = parent_keys[middle]
            right = self._allocate((False, parent_keys[middle + 1:], children[middle + 1:], 0))
            self._write_node(parent, (False, parent_keys[:middle], children[:middle + 1], 0))
            page = parent
        self.root = self._allocate((False, [separator], [page, right], 0))
        self._write_header()

    def delete(self, key):
        if self.get(key) is not None:
            self.put(key, TOMBSTONE)

    def items(self):
        page = self.root
        while True:
            leaf, keys, values, next_leaf = self._read_node(page)
            if leaf:
                break
            page = values[0]
        while True:
            for key, value in zip(keys, values):
                if value != TOMBSTONE:
                    yield key, value
            if not next_leaf:
                return
            leaf, keys, values, next_leaf = self._read_node(next_leaf)
            if not leaf:
                raise ValueError("invalid leaf chain")

    def close(self):
        self.file.close()
