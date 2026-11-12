"""Bounded optional NumPy arithmetic cache; no external search/index engine."""
import heapq
import math
import os
import sys


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
        count = 0
        for file in files:
            keys = database.path / (file + '.keys')
            if not keys.exists():
                return None
            count += keys.stat().st_size // 8
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
        obj.size = obj.vectors.nbytes + 256
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

    def topk(self, conditions, query, count):
        np = self.np
        mask = np.ones(len(self.rows), dtype=bool) if conditions else None
        functions = {'=': np.equal, '<': np.less, '<=': np.less_equal,
                     '>': np.greater, '>=': np.greater_equal}
        for name, operator, value in conditions:
            mask &= functions[operator](self.scalar[name], value)
        indexes = np.flatnonzero(mask) if mask is not None else None
        size = len(self.rows) if indexes is None else len(indexes)
        if not size or count == 0:
            return []
        distances = np.empty(size, dtype=np.float64)
        query = np.asarray(query, dtype=np.float64)
        # Float64 subtraction avoids float32 overflow and cancellation from
        # norm(x)^2 + norm(q)^2 - 2 dot(x,q). Bound temporary matrices to 512 rows.
        for start in range(0, size, 512):
            end = min(start + 512, size)
            vectors = self.vectors[start:end] if indexes is None else self.vectors[indexes[start:end]]
            delta = np.subtract(vectors, query, dtype=np.float64)
            np.einsum('ij,ij->i', delta, delta, out=distances[start:end])
        # Include the entire boundary tie before primary-key ordering. Pure
        # argpartition[:K] would pick arbitrary IDs for equal-distance vectors.
        if size > count:
            boundary = np.partition(distances, count-1)[count-1]
            tolerance = abs(boundary) * np.finfo(np.float64).eps * max(1, len(query)) * 16
            candidates = np.flatnonzero(distances <= np.nextafter(boundary + tolerance, np.inf))
        else:
            candidates = range(size)
        def exact_key(position):
            index = int(position if indexes is None else indexes[position])
            vector = self.vectors[index]
            distance = math.sqrt(math.fsum((float(a)-float(b))**2 for a,b in zip(vector,query)))
            return distance, int(self.keys[index])
        # Re-evaluate the candidate boundary using the same accurate scalar sum
        # as the reference path. SIMD reduction order must not change tied IDs.
        chosen_positions = heapq.nsmallest(count, candidates, key=exact_key)
        result = []
        for position in chosen_positions:
            index = int(position if indexes is None else indexes[position])
            row = list(self.rows[index])
            row[self.column] = self.vectors[index].tolist()
            result.append(row)
        return result
