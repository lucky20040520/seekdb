"""Bounded optional NumPy arithmetic cache; no external search/index engine."""
import heapq
import math
import os
import sys
from collections import OrderedDict


class VectorCache:
    @classmethod
    def build(cls, database, table, budget):
        if os.environ.get("MINIVECTOR_NO_NUMPY") == "1":
            return None
        # Do not import NumPy for non-vector queries or oversized tables.
        column = next((i for i, c in enumerate(table['columns']) if c['kind'] == 'VECTOR'), None)
        if column is None:
            return None
        files = database.files(table)
        from . import segments
        count = 0
        binary_payload = 0
        for file in files:
            path = database.path / file
            segment_count = segments.row_count(path)
            if segment_count is None:
                return None
            count += segment_count
            if path.suffix == '.mvb':
                binary_payload += segments.payload_size(path)
        # Oversized scalar payloads cannot fit either. Reject before repeatedly
        # decoding wide rows or importing NumPy on the streaming/index path.
        if binary_payload > budget:
            return None
        dimension = table['columns'][column]['size']
        if count * (dimension * 4 + 128) > budget:
            return None
        try:
            import numpy as np
        except ImportError:
            return None
        obj = cls()
        obj.np, obj.column, obj.table = np, column, table
        obj.vectors = np.empty((count, dimension), dtype=np.float32)
        obj.rows = []
        obj.postings = OrderedDict()
        obj.posting_bytes = 0
        obj.budget = budget
        obj.size = obj.vectors.nbytes + 256 + sys.getsizeof(obj.postings)
        for row in database.rows(table):
            obj.vectors[len(obj.rows)] = row[column]
            row[column] = None
            metadata = tuple(row)
            obj.rows.append(metadata)
            obj.size += sys.getsizeof(metadata) + sum(sys.getsizeof(v) for v in metadata) + 16
            if obj.size > budget:
                return None
        obj.scalar = {}
        for i, c in enumerate(table['columns']):
            if i != column:
                dtype = np.int64 if c['kind'] == 'BIGINT' else object
                array = np.array([row[i] for row in obj.rows], dtype=dtype)
                obj.scalar[c['name']] = array
                obj.size += array.nbytes
                if c['primary']:
                    obj.keys = array
        if obj.size > budget:
            return None
        return obj

    def candidate_indexes(self, conditions):
        np = self.np
        if not conditions:
            return None
        # Cache only complete equality predicates. Entries belong to this
        # immutable table snapshot and share its total cache byte budget.
        signature = tuple(tuple(condition) for condition in conditions)
        memoize = all(operator == '=' for _, operator, _ in conditions)
        if memoize and signature in self.postings:
            value = self.postings.pop(signature)
            self.postings[signature] = value
            return value[0]
        mask = np.ones(len(self.rows), dtype=bool)
        functions = {'=': np.equal, '<': np.less, '<=': np.less_equal,
                     '>': np.greater, '>=': np.greater_equal}
        for name, operator, value in conditions:
            mask &= functions[operator](self.scalar[name], value)
        indexes = np.flatnonzero(mask)
        if memoize:
            cost = (indexes.nbytes + sys.getsizeof(indexes) + 256
                    + sys.getsizeof(signature)
                    + sum(sys.getsizeof(condition) + sum(map(sys.getsizeof, condition))
                          for condition in signature))
            data_size = self.size - self.posting_bytes
            capacity = min(1024 * 1024, max(0, self.budget - data_size))
            if cost <= capacity:
                while self.postings and (len(self.postings) >= 32 or self.posting_bytes + cost > capacity):
                    _, (_, removed) = self.postings.popitem(last=False)
                    self.posting_bytes -= removed
                    self.size -= removed
                self.postings[signature] = (indexes, cost)
                self.posting_bytes += cost
                self.size += cost
        return indexes

    def topk(self, conditions, query, count, include_vector=True):
        np = self.np
        indexes = self.candidate_indexes(conditions)
        size = len(self.rows) if indexes is None else len(indexes)
        if not size or count == 0:
            return []
        distances = np.empty(size, dtype=np.float64)
        query_array = np.asarray(query, dtype=np.float64)
        scratch = np.empty((min(size, 512), len(query)), dtype=np.float64)
        # Float64 subtraction avoids float32 overflow and cancellation from
        # norm(x)^2 + norm(q)^2 - 2 dot(x,q). Bound temporary matrices to 512 rows.
        for start in range(0, size, 512):
            end = min(start + 512, size)
            vectors = self.vectors[start:end] if indexes is None else self.vectors[indexes[start:end]]
            delta = scratch[:end-start]
            np.subtract(vectors, query_array, out=delta, dtype=np.float64)
            np.einsum('ij,ij->i', delta, delta, out=distances[start:end])
        # Include the entire boundary tie before primary-key ordering. Pure
        # argpartition[:K] would pick arbitrary IDs for equal-distance vectors.
        if size > count:
            boundary = np.partition(distances, count-1)[count-1]
            tolerance = abs(boundary) * np.finfo(np.float64).eps * max(1, len(query)) * 16
            candidates = np.flatnonzero(distances <= np.nextafter(boundary + tolerance, np.inf))
        else:
            candidates = range(size)
        def exact_candidates():
            for position in candidates:
                index = int(position if indexes is None else indexes[position])
                # Convert only the boundary candidates, once. Python float
                # arithmetic retains the reference math.fsum distance exactly.
                vector = self.vectors[index].tolist()
                distance = math.sqrt(math.fsum((a-b)**2 for a,b in zip(vector,query)))
                yield distance, int(self.keys[index]), index
        # Re-evaluate the candidate boundary using the same accurate scalar sum
        # as the reference path. SIMD reduction order must not change tied IDs.
        result = []
        for distance, _, index in heapq.nsmallest(count, exact_candidates()):
            row = list(self.rows[index])
            if include_vector:
                row[self.column] = self.vectors[index].tolist()
            result.append((row, distance))
        return result
