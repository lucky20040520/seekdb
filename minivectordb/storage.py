"""Append-only durable storage and exact vector search for MiniVectorDB.

The SQL and competition wire protocol are deliberately separate from this API.
"""
import heapq
import hashlib
import itertools
import json
import math
import os
import struct
import tempfile
import re
import uuid
import zlib
from pathlib import Path
from .page_index import PageIndex
from .page_data import PageData
from .transaction import Pending, encode
from .ann import ProjectionIndex


class DatabaseError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.code = {"duplicate primary key": "duplicate_key",
                     "vector dimension mismatch": "dimension_mismatch",
                     "unsupported SQL statement": "unsupported_sql"}.get(message, "database_error")


def _vector(value, dimension):
    if not isinstance(value, (list, tuple)) or len(value) != dimension:
        raise DatabaseError("vector dimension mismatch")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise DatabaseError("invalid vector value")
        try:
            value32 = struct.unpack("<f", struct.pack("<f", item))[0]
        except (OverflowError, struct.error):
            raise DatabaseError("vector value outside float32 range") from None
        if not math.isfinite(value32):
            raise DatabaseError("vector values must be finite")
        result.append(value32)
    return result


class Database:
    """Single-process database with a redo log and rebuildable disk indexes."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.directory / "checkpoint.manifest"
        self.checkpoint_path = None
        self.checkpoint_pages = None
        self.checkpoint_hash = None
        if self.manifest_path.exists():
            with self.manifest_path.open("r", encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            version = manifest.get("version")
            expected = {"version", "snapshot", "wal"} if version == 1 else {"version", "snapshot", "pages", "pages_sha256", "wal"}
            if set(manifest) != expected or version not in (1, 2):
                raise DatabaseError("invalid checkpoint manifest")
            if not re.fullmatch(r"snapshot-[0-9a-f]{32}\.wal", manifest["snapshot"]) or not re.fullmatch(r"wal-[0-9a-f]{32}\.wal", manifest["wal"]):
                raise DatabaseError("invalid checkpoint file name")
            self.checkpoint_path = self.directory / manifest["snapshot"]
            self.path = self.directory / manifest["wal"]
            if version == 2:
                if not re.fullmatch(r"snapshot-[0-9a-f]{32}\.pages", manifest["pages"]) or not re.fullmatch(r"[0-9a-f]{64}", manifest["pages_sha256"]):
                    raise DatabaseError("invalid checkpoint page metadata")
                self.checkpoint_pages = self.directory / manifest["pages"]
                self.checkpoint_hash = manifest["pages_sha256"]
                if not self.checkpoint_pages.is_file():
                    raise DatabaseError("checkpoint pages missing")
            if not self.checkpoint_path.is_file() or not self.path.is_file():
                raise DatabaseError("checkpoint files missing")
        else:
            self.path = self.directory / "data.wal"
        new = not self.path.exists()
        self.file = open(self.path, "a+b")
        if new:
            self.file.flush()
            os.fsync(self.file.fileno())
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        self.schemas = {}
        self.indexes = {}
        self.vector_indexes = {}
        self.vector_runtime = {}
        self.data = PageData(self.directory)
        self.pending = None
        self.failed = False
        self.checkpoint_bytes = 16 * 1024 * 1024
        if self.checkpoint_path is not None:
            if self.checkpoint_pages is None:
                with self.checkpoint_path.open("rb") as snapshot:
                    self._recover(snapshot, truncate=False)
            else:
                with self.checkpoint_pages.open("rb") as pages:
                    self.data.load(pages, self.checkpoint_hash)
                with self.checkpoint_path.open("rb") as snapshot:
                    self._restore_checkpoint(snapshot)
        self._recover(self.file, truncate=True)
        for name in self.vector_indexes:
            self._build_vector_index(name)

    def _restore_checkpoint(self, snapshot):
        if snapshot.readline() != b'["begin"]\n':
            raise DatabaseError("invalid checkpoint header")
        start, checksum = snapshot.tell(), 0
        while True:
            line = snapshot.readline()
            if not line or not line.endswith(b"\n"):
                raise DatabaseError("incomplete checkpoint")
            op = json.loads(line)
            if isinstance(op, list) and len(op) == 2 and op[0] == "commit":
                if op[1] != checksum or snapshot.read(1):
                    raise DatabaseError("invalid checkpoint checksum")
                break
            if not isinstance(op, list) or not op or op[0] not in ("schema", "ref", "vector_index"):
                raise DatabaseError("invalid checkpoint entry")
            checksum = zlib.crc32(line, checksum)
        snapshot.seek(start)
        while True:
            op = json.loads(snapshot.readline())
            if op[0] == "commit":
                break
            if op[0] == "ref":
                if not 0 <= op[3] < self.data.end:
                    raise DatabaseError("invalid checkpoint row offset")
                self.indexes[op[1]].put(op[2], op[3])
            else:
                self._index([op], 0)

    def _recover(self, source, truncate):
        source.seek(0)
        last_good = 0
        while True:
            start = source.tell()
            line = source.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            try:
                marker = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise DatabaseError("corrupt WAL") from exc
            if marker != ["begin"]:
                raise DatabaseError("corrupt WAL transaction header")
            data_start = source.tell()
            checksum = 0
            committed = False
            while True:
                record = source.readline()
                if not record or not record.endswith(b"\n"):
                    break
                try:
                    value = json.loads(record)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise DatabaseError("corrupt WAL record") from exc
                if isinstance(value, list) and len(value) == 2 and value[0] == "commit":
                    if value[1] != checksum:
                        raise DatabaseError("WAL checksum mismatch")
                    end = source.tell()
                    self._replay(source, data_start, end)
                    source.seek(end)
                    last_good = end
                    committed = True
                    break
                if not isinstance(value, list) or not value or value[0] not in ("schema", "put", "delete", "vector_index"):
                    raise DatabaseError("invalid WAL record")
                checksum = zlib.crc32(record, checksum)
            if not committed:
                source.seek(start)
                break
        source.seek(0, os.SEEK_END)
        if not truncate and source.tell() != last_good:
            raise DatabaseError("incomplete checkpoint")
        if truncate:
            source.truncate(last_good)
        source.seek(0, os.SEEK_END)

    def _replay(self, source, start, end):
        source.seek(start)
        while source.tell() < end:
            offset = source.tell()
            op = json.loads(source.readline())
            if op[0] == "commit":
                break
            self._index([op], offset)

    def _index(self, operations, offset):
        for op in operations:
            if op[0] == "schema":
                self.schemas[op[1]] = op[2]
                if op[1] in self.indexes:
                    self.indexes[op[1]].close()
                filename = hashlib.sha256(op[1].encode("utf-8")).hexdigest() + ".idx"
                self.indexes[op[1]] = PageIndex(self.directory / filename, create=True)
            elif op[0] == "put":
                row_offset = self.data.append(self.schemas[op[1]], op[3])
                self.indexes[op[1]].put(op[2], row_offset)
                self._invalidate_vector_indexes(op[1])
            elif op[0] == "delete":
                self.indexes[op[1]].delete(op[2])
                self._invalidate_vector_indexes(op[1])
            elif op[0] == "vector_index":
                self.vector_indexes[op[1]] = (op[2], op[3])
            else:
                raise DatabaseError("unknown WAL operation")

    def begin(self):
        if self.failed:
            raise DatabaseError("database must be reopened after an I/O failure")
        if self.pending is not None:
            raise DatabaseError("transaction already active")
        self.pending = Pending(self.directory)

    def commit(self):
        if self.pending is None:
            raise DatabaseError("no active transaction")
        operations = self.pending
        if operations.count:
            try:
                self.file.seek(0, os.SEEK_END)
                self.file.write(b'["begin"]\n')
                start = self.file.tell()
                checksum = 0
                for record in operations.records():
                    self.file.write(record)
                    checksum = zlib.crc32(record, checksum)
                self.file.write(json.dumps(["commit", checksum], separators=(",", ":")).encode("ascii") + b"\n")
                self.file.flush()
                os.fsync(self.file.fileno())
                end = self.file.tell()
                self._replay(self.file, start, end)
                self.file.seek(end)
            except (OSError, ValueError) as exc:
                self.failed = True
                raise DatabaseError("commit failed; reopen database to recover") from exc
        operations.close()
        self.pending = None
        if self.file.tell() >= self.checkpoint_bytes:
            self.checkpoint()

    def checkpoint(self):
        """Publish a compact immutable snapshot and switch to a fresh WAL.

        Existing files remain intact until the new manifest is durable. No
        checkpoint is allowed while an explicit transaction is active.
        """
        if self.failed:
            raise DatabaseError("database must be reopened after an I/O failure")
        if self.pending is not None:
            raise DatabaseError("cannot checkpoint an active transaction")
        token = uuid.uuid4().hex
        snapshot_path = self.directory / ("snapshot-" + token + ".wal")
        pages_path = self.directory / ("snapshot-" + token + ".pages")
        wal_path = self.directory / ("wal-" + token + ".wal")
        manifest_tmp = self.directory / ("manifest-" + token + ".tmp")
        old_wal, old_snapshot, old_pages = self.path, self.checkpoint_path, self.checkpoint_pages
        published = False
        compact = PageData(self.directory)
        try:
            with snapshot_path.open("xb") as snapshot:
                snapshot.write(b'["begin"]\n')
                checksum = 0
                for table, schema in self.schemas.items():
                    record = encode(["schema", table, schema])
                    snapshot.write(record)
                    checksum = zlib.crc32(record, checksum)
                    for row in self.rows(table):
                        offset = compact.append(schema, row)
                        record = encode(["ref", table, row[schema["primary_key"]], offset])
                        snapshot.write(record)
                        checksum = zlib.crc32(record, checksum)
                for name, (table, column) in self.vector_indexes.items():
                    record = encode(["vector_index", name, table, column])
                    snapshot.write(record)
                    checksum = zlib.crc32(record, checksum)
                snapshot.write(encode(["commit", checksum]))
                snapshot.flush()
                os.fsync(snapshot.fileno())
            with pages_path.open("xb") as pages:
                pages_hash = compact.export(pages)
                pages.flush()
                os.fsync(pages.fileno())
            with wal_path.open("xb") as new_wal:
                new_wal.flush()
                os.fsync(new_wal.fileno())
            with manifest_tmp.open("x", encoding="utf-8") as manifest:
                json.dump({"version": 2, "snapshot": snapshot_path.name, "pages": pages_path.name,
                           "pages_sha256": pages_hash, "wal": wal_path.name}, manifest)
                manifest.flush()
                os.fsync(manifest.fileno())
            self._sync_directory()
            os.replace(manifest_tmp, self.manifest_path)
            published = True
            self._sync_directory()
            new_file = wal_path.open("a+b")
            self.file.close()
            self.file = new_file
            self.path, self.checkpoint_path, self.checkpoint_pages = wal_path, snapshot_path, pages_path
            self.checkpoint_hash = pages_hash
            # The durable manifest points only to the new files. Failed cleanup
            # leaves harmless old generations for manual inspection.
            for obsolete in (old_wal, old_snapshot, old_pages):
                if obsolete is not None and obsolete not in (self.path, self.checkpoint_path, self.checkpoint_pages):
                    try:
                        obsolete.unlink()
                    except OSError:
                        pass
            self._sync_directory()
        except (OSError, ValueError) as exc:
            if published:
                self.failed = True
            raise DatabaseError("checkpoint failed; reopen database if published") from exc
        finally:
            compact.close()

    def _sync_directory(self):
        if os.name != "nt":
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def rollback(self):
        if self.pending is None:
            raise DatabaseError("no active transaction")
        self.pending.close()
        self.pending = None

    def _apply(self, operations):
        if self.failed:
            raise DatabaseError("database must be reopened after an I/O failure")
        # A statement is staged completely before it changes the transaction.
        # Validation errors thus leave previous statements untouched.
        with tempfile.TemporaryFile(dir=self.directory, mode="w+b") as stage:
            count = 0
            for op in operations:
                stage.write(encode(op))
                count += 1
            automatic = self.pending is None
            if automatic:
                self.begin()
            try:
                stage.seek(0)
                for line in stage:
                    self.pending.append(json.loads(line))
            except (OSError, ValueError) as exc:
                self.failed = True
                raise DatabaseError("transaction staging failed; reopen database") from exc
            if automatic:
                self.commit()
            return count

    def schema(self, table):
        if self.failed:
            raise DatabaseError("database must be reopened after an I/O failure")
        if self.pending is not None and table in self.pending.schemas:
            return self.pending.schemas[table]
        if table not in self.schemas:
            raise DatabaseError("unknown table")
        return self.schemas[table]

    def create_table(self, table, columns, primary_key):
        if table in self.schemas or (self.pending is not None and table in self.pending.schemas):
            raise DatabaseError("table already exists")
        if primary_key not in columns or columns[primary_key] != ["BIGINT", None]:
            raise DatabaseError("primary key must be BIGINT")
        vectors = 0
        for kind, size in columns.values():
            if kind not in ("BIGINT", "VARCHAR", "VECTOR"):
                raise DatabaseError("invalid column type")
            if kind != "BIGINT" and (not isinstance(size, int) or isinstance(size, bool) or size < 1):
                raise DatabaseError("invalid column size")
            vectors += kind == "VECTOR"
        if vectors > 1:
            raise DatabaseError("at most one vector column")
        self._apply([["schema", table, {"columns": columns, "primary_key": primary_key}]])

    def create_vector_index(self, name, table, column):
        schema = self.schema(table)
        if name in self.vector_indexes or (self.pending is not None and name in self.pending.vector_indexes):
            raise DatabaseError("vector index already exists")
        if column not in schema["columns"] or schema["columns"][column][0] != "VECTOR":
            raise DatabaseError("vector index requires a vector column")
        self._apply([["vector_index", name, table, column]])

    def _invalidate_vector_indexes(self, table):
        for name, (index_table, _) in self.vector_indexes.items():
            if index_table == table and name in self.vector_runtime:
                self.vector_runtime.pop(name).close()

    def _build_vector_index(self, name):
        table, column = self.vector_indexes[name]
        if name in self.vector_runtime:
            self.vector_runtime.pop(name).close()
        self.vector_runtime[name] = ProjectionIndex(self, table, column, self.schema(table)["columns"][column][1])

    def _validate_value(self, kind, size, value):
        if kind == "BIGINT":
            if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 63) <= value < (1 << 63):
                raise DatabaseError("invalid BIGINT")
        elif kind == "VARCHAR":
            if not isinstance(value, str) or len(value) > size:
                raise DatabaseError("invalid VARCHAR")
        else:
            value = _vector(value, size)
        return value

    def _validate(self, schema, row):
        if set(row) != set(schema["columns"]):
            raise DatabaseError("columns do not match table")
        return {name: self._validate_value(kind, size, row[name])
                for name, (kind, size) in schema["columns"].items()}

    def get(self, table, key):
        self.schema(table)
        if self.pending is not None:
            op = self.pending.get(table, key)
            if op is not None:
                return op[3] if op[0] == "put" else None
        index = self.indexes.get(table)
        offset = index.get(key) if index else None
        if offset is None:
            return None
        return self.data.read(self.schema(table), offset)

    def insert(self, table, rows):
        schema = self.schema(table)
        with tempfile.TemporaryFile(dir=self.directory, mode="w+b") as index_file:
            seen = PageIndex(fileobj=index_file, create=True, cache_pages=8)
            def operations():
                for row in rows:
                    row = self._validate(schema, row)
                    key = row[schema["primary_key"]]
                    if seen.get(key) is not None or self.get(table, key) is not None:
                        raise DatabaseError("duplicate primary key")
                    seen.put(key, 1)
                    yield ["put", table, key, row]
            self._apply(operations())

    def rows(self, table):
        self.schema(table)
        pending_index = self.pending.indexes.get(table) if self.pending is not None else None
        pending_keys = (key for key, _ in pending_index.items()) if pending_index else iter(())
        index = self.indexes.get(table)
        committed = (key for key, _ in index.items()) if index else iter(())
        keys = heapq.merge(committed, pending_keys)
        for key, _ in itertools.groupby(keys):
            row = self.get(table, key)
            if row is not None:
                yield row

    def update(self, table, predicate, changes):
        schema = self.schema(table)
        if schema["primary_key"] in changes:
            raise DatabaseError("primary key update not supported")
        if not set(changes) <= set(schema["columns"]):
            raise DatabaseError("unknown column")
        changes = {name: self._validate_value(*schema["columns"][name], value)
                   for name, value in changes.items()}
        def operations():
            for row in self.rows(table):
                if predicate(row):
                    row.update(changes)
                    row = self._validate(schema, row)
                    yield ["put", table, row[schema["primary_key"]], row]
        return self._apply(operations())

    def delete(self, table, predicate):
        schema = self.schema(table)
        ops = (["delete", table, row[schema["primary_key"]]] for row in self.rows(table) if predicate(row))
        return self._apply(ops)

    def search(self, table, column, query, limit, predicate=lambda row: True, *, mode="exact", candidate_budget=None):
        schema = self.schema(table)
        kind, size = schema["columns"].get(column, (None, None))
        if kind != "VECTOR" or not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise DatabaseError("invalid vector search")
        query = _vector(query, size)
        if mode not in ("exact", "ann"):
            raise DatabaseError("invalid search mode")
        if candidate_budget is not None and candidate_budget != "auto" and (type(candidate_budget) is not int or candidate_budget < 1):
            raise DatabaseError("invalid candidate budget")
        if mode == "ann" and self.pending is None:
            name = next((name for name, target in self.vector_indexes.items() if target == (table, column)), None)
            if name is not None:
                if name not in self.vector_runtime:
                    self._build_vector_index(name)
                index = self.vector_runtime[name]
                budget = max(256, limit, (index.count * 93 + 99) // 100) if candidate_budget == "auto" else candidate_budget
                return index.search(query, limit, predicate, budget)
        primary_key = schema["primary_key"]
        def results():
            for row in self.rows(table):
                if predicate(row):
                    distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(row[column], query)))
                    yield (distance, row[primary_key], row)
        return heapq.nsmallest(limit, results(), key=lambda result: (result[0], result[1]))

    def close(self):
        if self.pending is not None:
            self.pending.close()
            self.pending = None
        for index in self.indexes.values():
            index.close()
        for index in self.vector_runtime.values():
            index.close()
        self.data.close()
        self.file.close()
