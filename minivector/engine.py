"""Disk-streaming immutable tables with atomic catalog publication.

No database or vector-index library is used. The catalog is the commit record;
unpublished immutable files have no effect on recovery.
"""
import copy
import heapq
import json
import math
import os
import struct
import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path


class DBError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(message, code="INVALID"):
    raise DBError(code, message)


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Database:
    def __init__(self, directory):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.path / "catalog.json"
        self.catalog = (json.loads(self.catalog_path.read_text(encoding="utf-8"))
                        if self.catalog_path.exists() else {})
        self.transaction = None
        self.query_cache = OrderedDict()
        self.query_cache_bytes = 0
        self.query_cache_limit = 16 * 1024 * 1024
        # Recovery only reads the last atomically published catalog. File deletion
        # is restartable: a crash during cleanup cannot change the committed set.
        self.collect()

    @property
    def visible(self):
        return self.catalog if self.transaction is None else self.transaction

    def collect(self):
        live = {file for t in self.catalog.values() for file in self.files(t)}
        if self.transaction is not None:
            live.update(file for t in self.transaction.values() for file in self.files(t))
        for signature in list(self.query_cache):
            if any(file not in live for file in signature):
                self.query_cache_bytes -= self.query_cache.pop(signature).size
        live.update(file + ".keys" for file in list(live))
        for pattern in ("rows-*.jsonl", "rows-*.keys", "sort-*.jsonl", "catalog-*.tmp"):
            for file in self.path.glob(pattern):
                if file.name not in live:
                    file.unlink()

    def publish(self, catalog):
        temporary = self.path / ("catalog-" + uuid.uuid4().hex + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            output.write(encode(catalog))
            output.flush()
            os.fsync(output.fileno())
        # Ensure table names are durable before publishing their references.
        sync_directory(self.path)
        os.replace(str(temporary), str(self.catalog_path))
        self.catalog = catalog
        # If directory fsync fails after replace, cleanup must still preserve
        # files referenced by the now-visible catalog before the process exits.
        sync_directory(self.path)

    def accept(self, catalog):
        if self.transaction is None:
            self.publish(catalog)
        else:
            self.transaction = catalog
        self.collect()

    def begin(self):
        if self.transaction is not None:
            fail("transaction already active", "TRANSACTION")
        self.transaction = copy.deepcopy(self.catalog)

    def commit(self):
        if self.transaction is None:
            fail("no active transaction", "TRANSACTION")
        self.publish(self.transaction)
        self.transaction = None
        self.collect()

    def rollback(self):
        if self.transaction is None:
            fail("no active transaction", "TRANSACTION")
        self.transaction = None
        self.collect()

    def close(self):
        self.transaction = None
        self.collect()

    def table(self, name):
        try:
            return self.visible[name]
        except KeyError:
            fail("unknown table: " + name, "SCHEMA")

    def create(self, name, columns):
        if name in self.visible:
            fail("table exists", "SCHEMA")
        names = [c["name"] for c in columns]
        if len(set(names)) != len(names) or not columns:
            fail("duplicate or empty columns", "SCHEMA")
        primary = [c for c in columns if c["primary"]]
        if len(primary) != 1 or primary[0]["kind"] != "BIGINT":
            fail("exactly one BIGINT primary key required", "SCHEMA")
        if sum(c["kind"] == "VECTOR" for c in columns) > 1:
            fail("at most one vector column", "SCHEMA")
        for column in columns:
            if column["kind"] not in ("BIGINT", "VARCHAR", "VECTOR"):
                fail("unsupported type", "SCHEMA")
            if column["kind"] != "BIGINT" and column["size"] <= 0:
                fail("positive type size required", "SCHEMA")
        catalog = copy.deepcopy(self.visible)
        catalog[name] = {"columns": columns, "file": None, "indexes": {}}
        self.accept(catalog)

    def rows(self, table):
        for file in self.files(table):
            with (self.path / file).open(encoding="utf-8") as source:
                for line in source:
                    yield json.loads(line)

    @staticmethod
    def files(table):
        return table.get("files", [table["file"]] if table["file"] else [])

    def primary_keys(self, table):
        pk = next(i for i, c in enumerate(table["columns"]) if c["primary"])
        for file in self.files(table):
            keys = self.path / (file + ".keys")
            if keys.exists():
                with keys.open("rb") as source:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        yield from (key[0] for key in struct.iter_unpack("<q", chunk))
            else:
                # Compatibility with the original correctness baseline.
                with (self.path / file).open(encoding="utf-8") as source:
                    for line in source:
                        yield json.loads(line)[pk]

    @staticmethod
    def validate(column, value):
        kind = column["kind"]
        if kind == "BIGINT":
            if type(value) is not int or not -(2**63) <= value < 2**63:
                fail("BIGINT value out of range or wrong type", "TYPE")
            return value
        if kind == "VARCHAR":
            if not isinstance(value, str) or len(value) > column["size"]:
                fail("VARCHAR type or length mismatch", "TYPE")
            # Surrogate code points are not valid Unicode scalar values.
            if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                fail("invalid Unicode string", "TYPE")
            return value
        if not isinstance(value, list) or len(value) != column["size"]:
            fail("VECTOR dimension mismatch", "TYPE")
        result = []
        for number in value:
            if type(number) not in (int, float):
                fail("VECTOR element must be numeric", "TYPE")
            try:
                stored = struct.unpack("<f", struct.pack("<f", number))[0]
            except (OverflowError, struct.error):
                fail("VECTOR float32 overflow", "TYPE")
            if not math.isfinite(stored):
                fail("VECTOR requires finite float32", "TYPE")
            result.append(stored)
        return result

    def rewrite(self, name, rows, append=False):
        catalog = copy.deepcopy(self.visible)
        filename = "rows-" + uuid.uuid4().hex + ".jsonl"
        target = self.path / filename
        key_target = self.path / (filename + ".keys")
        pk = next(i for i, c in enumerate(catalog[name]["columns"]) if c["primary"])
        try:
            with target.open("w", encoding="utf-8", newline="\n") as output, key_target.open("wb") as keys:
                for row in rows:
                    output.write(encode(row) + "\n")
                    keys.write(struct.pack("<q", row[pk]))
                output.flush()
                os.fsync(output.fileno())
                keys.flush()
                os.fsync(keys.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            key_target.unlink(missing_ok=True)
            raise
        if append:
            catalog[name]["files"] = list(self.files(catalog[name])) + [filename]
            catalog[name]["file"] = None
        else:
            catalog[name].pop("files", None)
            catalog[name]["file"] = filename
        self.accept(catalog)

    def insert(self, name, values, column_names=None):
        table = self.table(name)
        columns = table["columns"]
        names = [c["name"] for c in columns]
        supplied = names if column_names is None else column_names
        if len(set(supplied)) != len(supplied) or set(supplied) != set(names):
            fail("INSERT must supply each column once", "SCHEMA")
        pk = next(i for i, c in enumerate(columns) if c["primary"])
        new = []
        keys = set()
        for values_row in values:
            if len(values_row) != len(supplied):
                fail("INSERT value count mismatch")
            row = [self.validate(c, values_row[supplied.index(c["name"])]) for c in columns]
            if row[pk] in keys:
                fail("duplicate primary key", "DUPLICATE")
            keys.add(row[pk])
            new.append(row)
        for key in self.primary_keys(table):
            if key in keys:
                fail("duplicate primary key", "DUPLICATE")
        self.rewrite(name, iter(new), append=True)
        return len(new)

    def conditions(self, table, conditions):
        columns = {c["name"]: (i, c) for i, c in enumerate(table["columns"])}
        compiled = []
        for name, operator, value in conditions:
            if name not in columns:
                fail("unknown filter column", "SCHEMA")
            i, column = columns[name]
            if column["kind"] == "VECTOR":
                fail("vector comparison unsupported")
            compiled.append((i, operator, self.validate(column, value)))
        def matches(row):
            for i, operator, value in compiled:
                actual = row[i]
                if not {"=": lambda: actual == value, "<": lambda: actual < value,
                        "<=": lambda: actual <= value, ">": lambda: actual > value,
                        ">=": lambda: actual >= value}[operator]():
                    return False
            return True
        return matches

    def modify(self, name, conditions, assignments=None):
        table = self.table(name)
        matches = self.conditions(table, conditions)
        changes = {}
        if assignments is not None:
            for field, value in assignments:
                indexes = [i for i, c in enumerate(table["columns"]) if c["name"] == field]
                if not indexes:
                    fail("unknown update column", "SCHEMA")
                i = indexes[0]
                if table["columns"][i]["primary"] or i in changes:
                    fail("primary key update or duplicate assignment unsupported")
                changes[i] = self.validate(table["columns"][i], value)
        count = [0]
        def modified():
            for row in self.rows(table):
                if matches(row):
                    count[0] += 1
                    if assignments is None:
                        continue
                    for i, value in changes.items():
                        row[i] = value
                yield row
        self.rewrite(name, modified())
        return count[0]

    def create_index(self, name, table_name, column):
        table = self.table(table_name)
        if any(name in t["indexes"] for t in self.visible.values()):
            fail("index exists", "SCHEMA")
        if not any(c["name"] == column and c["kind"] == "VECTOR" for c in table["columns"]):
            fail("index requires VECTOR column", "SCHEMA")
        catalog = copy.deepcopy(self.visible)
        # Exact scan index baseline: no stale derived state on update or rollback.
        catalog[table_name]["indexes"][name] = {"column": column, "algorithm": "exact_scan"}
        self.accept(catalog)

    def cached_vectors(self, table):
        signature = tuple(self.files(table))
        if signature in self.query_cache:
            value = self.query_cache.pop(signature)
            self.query_cache[signature] = value
            return value
        from .vector import VectorCache
        # Evict old entries before building a new bounded cache, avoiding two
        # complete table caches during transaction visibility changes.
        self.query_cache.clear()
        self.query_cache_bytes = 0
        cache = VectorCache.build(self, table, self.query_cache_limit)
        if cache is not None:
            self.query_cache[signature] = cache
            self.query_cache_bytes = cache.size
        return cache

    def external_sort(self, rows, key):
        files = []
        readers = []
        try:
            batch, byte_count = [], 0
            def spill():
                batch.sort(key=key)
                handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(self.path),
                                                     prefix="sort-", suffix=".jsonl", delete=False)
                files.append(Path(handle.name))
                with handle:
                    for row in batch:
                        handle.write(encode(row) + "\n")
            for row in rows:
                batch.append(row)
                byte_count += len(encode(row).encode("utf-8"))
                if byte_count >= 2 * 1024 * 1024:
                    spill()
                    batch, byte_count = [], 0
            if not files:
                yield from sorted(batch, key=key)
                return
            if batch:
                spill()
            # Bounded fan-in keeps file descriptors and merge heap independent
            # of total table size. Intermediate files remain inside /data.
            while len(files) > 32:
                merged_files = []
                for start in range(0, len(files), 32):
                    group = files[start:start + 32]
                    output = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(self.path),
                                                         prefix="sort-", suffix=".jsonl", delete=False)
                    merged = Path(output.name)
                    try:
                        readers = [file.open(encoding="utf-8") for file in group]
                        with output:
                            for row in heapq.merge(*(map(json.loads, reader) for reader in readers), key=key):
                                output.write(encode(row) + "\n")
                    except BaseException:
                        output.close()
                        merged.unlink(missing_ok=True)
                        raise
                    finally:
                        for reader in readers:
                            reader.close()
                        readers = []
                    merged_files.append(merged)
                for file in files:
                    file.unlink()
                files = merged_files
            for file in files:
                readers.append(file.open(encoding="utf-8"))
            yield from heapq.merge(*(map(json.loads, reader) for reader in readers), key=key)
        finally:
            for reader in readers:
                reader.close()
            for file in files:
                file.unlink(missing_ok=True)

    def select(self, name, projections, conditions=(), order=None, limit=None, mode="exact"):
        if mode not in ("exact", "ann"):
            fail("unknown search mode")
        table = self.table(name)
        columns = {c["name"]: (i, c) for i, c in enumerate(table["columns"])}
        pk = next(i for i, c in enumerate(table["columns"]) if c["primary"])
        matches = self.conditions(table, conditions)
        aliases = {}
        def expression(expr, resolve_alias=False):
            kind = expr[0]
            if kind == "column":
                field = expr[1]
                if resolve_alias and field in aliases:
                    return aliases[field]
                if field not in columns:
                    fail("unknown projection/order column", "SCHEMA")
                index, column = columns[field]
                return lambda row: row[index]
            if kind != "distance" or expr[1] not in columns:
                fail("invalid distance expression", "SCHEMA")
            index, column = columns[expr[1]]
            if column["kind"] != "VECTOR":
                fail("distance requires VECTOR", "TYPE")
            query = self.validate(column, expr[2])
            return lambda row: math.sqrt(math.fsum((a-b)**2 for a, b in zip(row[index], query)))
        if projections == "*":
            projections = [(('column', c["name"]), c["name"]) for c in table["columns"]]
        functions, headers = [], []
        for expr, alias in projections:
            fn = expression(expr)
            functions.append(fn)
            headers.append(alias)
            if alias:
                aliases[alias] = fn
        if limit is not None and (type(limit) is not int or limit < 0):
            fail("nonnegative integer LIMIT required")
        source = (row for row in self.rows(table) if matches(row))
        if order is not None:
            expr, descending = order
            if expr[0] == "column" and expr[1] not in aliases and expr[1] in columns and columns[expr[1]][1]["kind"] == "VECTOR":
                fail("ordering VECTOR unsupported")
            order_fn = expression(expr, resolve_alias=True)
            distance_expr = expr
            if expr[0] == "column":
                distance_expr = next((projection for projection, alias in projections if alias == expr[1]), expr)
            if distance_expr[0] == "column" and distance_expr[1] in columns and columns[distance_expr[1]][1]["kind"] == "VECTOR":
                fail("ordering VECTOR unsupported")
            cache = None
            if distance_expr[0] == "distance" and not descending and limit is not None and limit <= 4096:
                cache = self.cached_vectors(table)
            class Desc:
                def __init__(self, value): self.value = value
                def __lt__(self, other): return self.value > other.value
                def __eq__(self, other): return self.value == other.value
            def key(row):
                value = order_fn(row)
                if isinstance(value, list):
                    fail("ordering VECTOR unsupported")
                return (Desc(value) if descending else value, row[pk])
            if cache is not None:
                query_column = columns[distance_expr[1]][1]
                query = self.validate(query_column, distance_expr[2])
                source = iter(cache.topk(conditions, query, limit))
            else:
                source = (iter(heapq.nsmallest(limit, source, key=key)) if limit is not None and limit <= 4096
                          else self.external_sort(source, key))
        def projected():
            try:
                if limit == 0:
                    return
                count = 0
                for row in source:
                    yield [fn(row) for fn in functions]
                    count += 1
                    if limit is not None and count >= limit:
                        break
            finally:
                if hasattr(source, "close"):
                    source.close()
        return headers, projected()
