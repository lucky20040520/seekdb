"""Disk-streaming immutable tables with journaled catalog publication.

No database or vector-index library is used. The catalog is the commit record;
unpublished immutable segments have no effect on recovery.
"""
import copy
import heapq
import json
import math
import os
import re
import struct
import sys
import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path

JOURNAL_REWRITE_LIMIT = 8 * 1024 * 1024


class DBError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(message, code="INVALID"):
    raise DBError(code, message)


# A vector literal that is exactly a JSON array of numbers: optional leading
# minus, no leading plus, no leading zeros, no bare '.', no trailing dot.
_VECTOR_NUMBER = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_VECTOR_LITERAL = re.compile(
    r"\[\s*(?:" + _VECTOR_NUMBER + r"(?:\s*,\s*" + _VECTOR_NUMBER + r")*)?\s*\]\Z")


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
        from .journal import Journal
        legacy = (json.loads(self.catalog_path.read_text(encoding="utf-8"))
                  if not (self.path/'CURRENT').exists() and self.catalog_path.exists() else {})
        self.journal = Journal(self.path, legacy, sync_directory)
        self.catalog = self.journal.catalog
        self._external_files = self.external_files(self.catalog)
        self.transaction = None
        self.query_cache = OrderedDict()
        self.query_cache_bytes = 0
        self.query_cache_limit = self.default_cache_limit()
        # Immutable-file key ranges avoid repeatedly opening disjoint segments.
        # This small metadata cache is optional and never a source of truth.
        self.key_ranges = OrderedDict()
        self.key_range_bytes = 0
        self.key_range_limit = 16 * 1024 * 1024
        # Recovery only reads the last complete journal commit. File deletion
        # is restartable: a crash during cleanup cannot change the committed set.
        self.collect()
        # Derived indexes may be rebuilt, but construction and durable publication
        # finish before this constructor returns and the CLI emits readiness.
        recovered = copy.deepcopy(self.catalog)
        if self.prepare_indexes(recovered):
            self.publish(recovered)
            self.collect()

    @property
    def visible(self):
        return self.catalog if self.transaction is None else self.transaction

    def collect(self):
        if self.journal.closed or self.journal.poisoned:
            return
        if self.transaction is None:
            self.journal.discard_tail()
            self.catalog = self.journal.checkpoint(self.catalog)
            self.journal.collect(self.catalog)
        live = {file for t in self.catalog.values() for file in self.files(t)}
        if self.transaction is not None:
            live.update(file for t in self.transaction.values() for file in self.files(t))
        for file in list(self.key_ranges):
            if file not in live:
                self.key_range_bytes -= self.key_ranges.pop(file)[2]
        for signature in list(self.query_cache):
            if any(file not in live for file in signature):
                self.query_cache_bytes -= self.query_cache.pop(signature).size
        live.update(file + ".keys" for file in list(live))
        catalogs = [self.catalog] + ([] if self.transaction is None else [self.transaction])
        for catalog in catalogs:
            for table in catalog.values():
                for descriptor in table['indexes'].values():
                    live.update(descriptor.get('segments',{}).values())
        for pattern in ("rows-*.jsonl", "rows-*.mvb", "rows-*.keys", "index-*.flat", "sort-*.jsonl", "catalog-*.tmp"):
            for file in self.path.glob(pattern):
                if file.name not in live:
                    file.unlink()

    def publish(self, catalog):
        external = self.external_files(catalog)
        added = external-self._external_files
        if added:
            # Legacy row files and derived disk indexes live outside the log.
            # Persist both their contents and names before publishing references.
            for name in sorted(added):
                with (self.path/name).open('r+b') as source:
                    os.fsync(source.fileno())
            sync_directory(self.path)
        self.journal.commit(catalog)
        self.catalog = catalog
        self._external_files = external

    @classmethod
    def external_files(cls, catalog):
        from .journal import reference
        result = {name for table in catalog.values() for name in cls.files(table) if reference(name) is None}
        for table in catalog.values():
            for descriptor in table['indexes'].values():
                result.update(descriptor.get('segments', {}).values())
        return result

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
        try:
            self.collect()
        finally:
            self.journal.close()

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
        from .segments import Reader
        for file in self.files(table):
            with Reader(self.path / file, table['columns']) as source:
                for _, row in source:
                    yield row

    @staticmethod
    def files(table):
        return table.get("files", [table["file"]] if table["file"] else [])

    def primary_keys(self, table):
        for file in self.files(table):
            yield from self.file_primary_keys(table, file)

    def file_primary_keys(self, table, file):
        from .segments import Reader
        pk = next(i for i, c in enumerate(table["columns"]) if c["primary"])
        keys = self.path / (file + ".keys")
        if keys.exists():
            with keys.open("rb") as source:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    yield from (key[0] for key in struct.iter_unpack("<q", chunk))
        else:
            # Binary footer, or legacy JSONL without a key sidecar.
            with Reader(self.path / file, table['columns']) as source:
                yield from source.keys(pk)

    def check_primary_keys(self, table, keys):
        if not keys:
            return
        lower, upper = min(keys), max(keys)
        for file in self.files(table):
            bounds = self.key_ranges.get(file)
            if bounds is not None:
                if bounds[0] is None or upper < bounds[0] or lower > bounds[1]:
                    continue
            # Reserve enough for two int64 extrema before learning a range.
            # On saturation keep useful existing entries rather than cycling
            # through every segment on each scan of a larger-than-cache table.
            cost = 256 + sys.getsizeof(file) + sys.getsizeof((0, 0, 0)) + 2 * sys.getsizeof(2**63)
            learn = bounds is None and self.key_range_bytes + cost + 256 <= self.key_range_limit
            minimum = maximum = None
            for key in self.file_primary_keys(table, file):
                if key in keys:
                    fail("duplicate primary key", "DUPLICATE")
                if learn:
                    minimum = key if minimum is None else min(minimum, key)
                    maximum = key if maximum is None else max(maximum, key)
            # Only a complete successful scan may supply a range. Failed or
            # interrupted reads cannot cache a partial range and hide duplicates.
            if learn:
                self.key_ranges[file] = (minimum, maximum, cost)
                self.key_range_bytes += cost

    @staticmethod
    def validate(column, value, for_storage=True):
        kind = column["kind"]
        if kind == "BIGINT":
            if type(value) is not int or not -(2**63) <= value < 2**63:
                fail("BIGINT value out of range or wrong type", "TYPE")
            return value
        if kind == "VARCHAR":
            if not isinstance(value, str) or (for_storage and len(value) > column["size"]):
                fail("VARCHAR type or length mismatch", "TYPE")
            # Surrogate code points are not valid Unicode scalar values.
            if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                fail("invalid Unicode string", "TYPE")
            return value
        # The published SQL samples encode VECTOR literals as quoted JSON
        # arrays. Decode only at a VECTOR-typed boundary so VARCHAR text such
        # as '[3,4]' remains an ordinary string.
        if isinstance(value, str):
            numeric = False
            text = value
            if text.isascii() and _VECTOR_LITERAL.match(text):
                # The literal is exactly a JSON array of numbers, which is what
                # every published sample and the benchmark emit. Parsing it
                # directly avoids a JSON decoder pass per row; anything else
                # (non-ASCII digits, underscores, bare '.5', 'true') falls back
                # to json.loads so the accepted grammar is unchanged.
                body = text[1:-1].strip()
                value = [] if not body else [float(part) for part in body.split(",")]
                numeric = True
            if not numeric:
                try:
                    value = json.loads(text)
                except ValueError:
                    fail("invalid VECTOR array literal", "TYPE")
        else:
            numeric = False
        if not isinstance(value, list) or len(value) != column["size"]:
            fail("VECTOR dimension mismatch", "TYPE")
        if not numeric and any(type(number) not in (int, float) for number in value):
            fail("VECTOR element must be numeric", "TYPE")
        # One standard-library conversion per vector, preserving the original
        # float32 rounding and rejecting booleans, overflow and nonfinite data.
        layout = '<%df' % len(value)
        try:
            stored = struct.unpack(layout, struct.pack(layout, *value))
        except (OverflowError, struct.error):
            fail("VECTOR float32 overflow", "TYPE")
        # Any infinite or NaN component makes the sum non-finite; a mix of
        # +inf and -inf already fails through the same check below.
        if not math.isfinite(sum(stored)):
            fail("VECTOR requires finite float32", "TYPE")
        return list(stored)

    def rewrite(self, name, rows, append=False):
        catalog = copy.deepcopy(self.visible)
        savepoint = self.journal.savepoint()
        external = False
        if not append:
            from . import segments
            size = 0
            for file in self.files(catalog[name]):
                path = self.path/file
                size += segments.payload_size(path) if path.suffix == '.mvb' else path.stat().st_size
                if size > JOURNAL_REWRITE_LIMIT:
                    external = True
                    break
        if external:
            # Large rewrites otherwise read and dirty the same log inode under
            # memory pressure. Stream into a separate immutable segment; publish
            # fsyncs its contents and name before the catalog commit as usual.
            self.journal.idle()
            filename = 'rows-'+uuid.uuid4().hex+'.mvb'
            try:
                with (self.path/filename).open('x+b') as output:
                    segments.write_stream(output, catalog[name]['columns'], rows, self.path)
            except BaseException:
                (self.path/filename).unlink(missing_ok=True)
                raise
        else:
            filename = self.journal.append_segment(catalog[name]['columns'], rows)
        if append:
            catalog[name]["files"] = list(self.files(catalog[name])) + [filename]
            catalog[name]["file"] = None
        else:
            catalog[name].pop("files", None)
            catalog[name]["file"] = filename
        try:
            self.prepare_indexes(catalog, only=name)
        except BaseException:
            self.journal.rewind(savepoint)
            self.journal.idle()
            if external:
                (self.path/filename).unlink(missing_ok=True)
            raise
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
        self.check_primary_keys(table, keys)
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
            # VARCHAR(n) constrains stored values, not a comparison literal.
            # For example a VARCHAR(2) value 'ab' can be compared with 'abc'.
            compiled.append((i, operator, self.validate(column, value, for_storage=False)))
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
        catalog[table_name]["indexes"][name] = {"column": column}
        self.prepare_indexes(catalog, only=table_name)
        self.accept(catalog)

    def prepare_indexes(self, catalog, only=None):
        from . import flat_index
        changed, created = False, []
        try:
            for name,table in catalog.items():
                if only is not None and name != only:
                    continue
                for descriptor in table['indexes'].values():
                    segments = {}
                    old = descriptor.get('segments',{}) if descriptor.get('algorithm')==flat_index.ALGORITHM else {}
                    for row_file in self.files(table):
                        index_file = old.get(row_file)
                        if not flat_index.valid(self.path,row_file,index_file,table,descriptor['column']):
                            index_file = flat_index.build(self.path,row_file,table,descriptor['column'],False)
                            created.append(index_file)
                        segments[row_file] = index_file
                    if descriptor.get('algorithm')!=flat_index.ALGORITHM or descriptor.get('segments')!=segments:
                        changed = True
                    descriptor['algorithm'] = flat_index.ALGORITHM
                    descriptor['segments'] = segments
            return changed
        except BaseException:
            for filename in created:
                (self.path/filename).unlink(missing_ok=True)
            raise

    def indexed_rows(self, table, descriptor, query, matches, conditions, limit, descending):
        from . import flat_index
        if limit == 0:
            return
        candidates = flat_index.candidates(self.path,table,descriptor,query,matches,bool(conditions))
        key = lambda value: (-value[0] if descending else value[0],value[1])
        if limit is not None and limit <= 4096:
            locations = iter(heapq.nsmallest(limit,candidates,key=key))
        else:
            locations = self.external_sort(candidates,key)
        count = 0
        try:
            for location in locations:
                yield flat_index.read_row(self.path,location,table)
                count += 1
                if limit is not None and count >= limit:
                    break
        finally:
            if hasattr(locations,'close'):
                locations.close()
            candidates.close()

    @staticmethod
    def default_cache_limit():
        """Bound for the in-memory vector cache, scaled to the container limit.

        A fixed 16 MiB keeps a 10k x 128 table cacheable but silently disables
        caching for 100k rows, where every query then rescans the table.
        """
        floor = 64 * 1024 * 1024
        ceiling = 256 * 1024 * 1024
        for name in ("/sys/fs/cgroup/memory.max",
                     "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                with open(name, encoding="utf-8") as handle:
                    text = handle.read().strip()
            except OSError:
                continue
            if not text or text == "max":
                continue
            try:
                limit = int(text)
            except ValueError:
                continue
            if limit <= 0 or limit > 1 << 60:
                continue
            return max(floor, min(ceiling, limit // 8))
        return floor

    def cached_vectors(self, table):
        signature = tuple(self.files(table))
        # Empty tables all have signature (). They need no arithmetic cache;
        # reusing it across distinct schemas would mix filter-column metadata.
        if not signature:
            return None
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
        distance_queries = []
        def distance_query(expr):
            # Reuse only this parsed expression, including ORDER BY aliases.
            # Value equality would incorrectly equate [1] with invalid [True].
            # The cache lasts one SELECT, never across schema/visibility changes.
            for previous, query in distance_queries:
                if previous is expr:
                    return query
            query = self.validate(columns[expr[1]][1], expr[2])
            distance_queries.append((expr, query))
            return query
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
            query = distance_query(expr)
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
        def filtered_rows():
            for row in self.rows(table):
                if matches(row):
                    yield row
        source = filtered_rows()
        cached_distance_columns = None
        if order is not None:
            expr, descending = order
            # Heap candidates retain full decoded rows, not just ordering keys.
            # Bound their declared worst-case size rather than only counting K.
            row_bytes = 64 + 8 * len(table["columns"])
            for column in table["columns"]:
                if column["kind"] == "VARCHAR":
                    row_bytes += 64 + 4 * column["size"]
                elif column["kind"] == "VECTOR":
                    row_bytes += 64 + 32 * column["size"]
                else:
                    row_bytes += 32
            bounded_candidates = (limit is not None and limit <= 4096
                                  and limit * row_bytes <= 2 * 1024 * 1024)
            if expr[0] == "column" and expr[1] not in aliases and expr[1] in columns and columns[expr[1]][1]["kind"] == "VECTOR":
                fail("ordering VECTOR unsupported")
            order_fn = expression(expr, resolve_alias=True)
            distance_expr = expr
            if expr[0] == "column":
                distance_expr = next((projection for projection, alias in projections if alias == expr[1]), expr)
            if distance_expr[0] == "column" and distance_expr[1] in columns and columns[distance_expr[1]][1]["kind"] == "VECTOR":
                fail("ordering VECTOR unsupported")
            cache = None
            descriptor = next((index for index in table['indexes'].values()
                               if distance_expr[0]=='distance' and index['column']==distance_expr[1]),None)
            # A durable index need not force the repeated Python scalar path.
            # Small snapshots use the same bounded NumPy exact scan with or
            # without an index; oversized snapshots retain the disk fallback.
            if distance_expr[0] == "distance" and not descending and bounded_candidates:
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
            if descriptor is not None and cache is None:
                query = distance_query(distance_expr)
                source = self.indexed_rows(table,descriptor,query,matches,conditions,limit,descending)
            elif cache is not None:
                query = distance_query(distance_expr)
                cached_distance_columns = [projection == distance_expr for projection, _ in projections]
                include_vector = any(
                    projection[1] == distance_expr[1] and not use_distance
                    for (projection, _), use_distance in zip(projections, cached_distance_columns)
                )
                source = iter(cache.topk(conditions, query, limit, include_vector))
                self.query_cache_bytes = cache.size
            else:
                source = (iter(heapq.nsmallest(limit, source, key=key)) if bounded_candidates
                          else self.external_sort(source, key))
        def projected():
            try:
                if limit == 0:
                    return
                if cached_distance_columns is not None:
                    for row, distance in source:
                        yield [distance if use_distance else fn(row)
                               for fn, use_distance in zip(functions, cached_distance_columns)]
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
