"""Disk-backed, single-writer transaction staging."""
import json
import tempfile

from .page_index import PageIndex


def encode(op):
    return json.dumps(op, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"


class Pending:
    def __init__(self, directory):
        self.directory = directory
        self.file = tempfile.TemporaryFile(dir=directory, mode="w+b")
        self.indexes = {}
        self.schemas = {}
        self.vector_indexes = {}
        self.count = 0

    def append(self, op):
        self.file.seek(0, 2)
        offset = self.file.tell()
        self.file.write(encode(op))
        if op[0] == "schema":
            self.schemas[op[1]] = op[2]
        elif op[0] == "vector_index":
            self.vector_indexes[op[1]] = (op[2], op[3])
        else:
            if op[1] not in self.indexes:
                index_file = tempfile.TemporaryFile(dir=self.directory, mode="w+b")
                self.indexes[op[1]] = PageIndex(fileobj=index_file, create=True, cache_pages=8)
            self.indexes[op[1]].put(op[2], offset)
        self.count += 1

    def get(self, table, key):
        index = self.indexes.get(table)
        offset = index.get(key) if index else None
        if offset is None:
            return None
        self.file.seek(offset)
        return json.loads(self.file.readline())

    def records(self):
        self.file.seek(0)
        while True:
            line = self.file.readline()
            if not line:
                return
            yield line

    def close(self):
        for index in self.indexes.values():
            index.close()
        self.file.close()
