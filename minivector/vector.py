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
        # Norms are optional snapshot data, subject to the same cache limit.
        # Float32 products are exact in float64; only the positive reduction
        # rounds. Do not retain a second copy of the vectors for filter groups.
        obj.norms = None
        norm_cost = count * 8 + 256
        if 16 <= dimension <= 4096 and count >= 128 and obj.size + norm_cost <= budget:
            obj.norms = np.einsum('ij,ij->i', obj.vectors, obj.vectors, dtype=np.float64)
            obj.max_norm = float(obj.norms.max(initial=0))
            obj.size += norm_cost
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
        functions = {'=': np.equal, '<': np.less, '<=': np.less_equal,
                     '>': np.greater, '>=': np.greater_equal}
        first, *remaining = conditions
        name, operator, value = first
        mask = functions[operator](self.scalar[name], value)
        for name, operator, value in remaining:
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

    def squared_distances(self, indexes, query, size, dtype):
        np = self.np
        distances = np.empty(size, dtype=np.float64)
        query_array = np.asarray(query, dtype=dtype)
        scratch = np.empty((min(size, 512), len(query)), dtype=dtype)
        with np.errstate(over='ignore', under='ignore', invalid='ignore'):
            for start in range(0, size, 512):
                end = min(start + 512, size)
                vectors = self.vectors[start:end] if indexes is None else self.vectors[indexes[start:end]]
                delta = scratch[:end-start]
                np.subtract(vectors, query_array, out=delta, dtype=dtype)
                if dtype is np.float64:
                    np.einsum('ij,ij->i', delta, delta, out=distances[start:end])
                else:
                    # Explicit dtype keeps the screening reduction in float32;
                    # the small result is widened for safe threshold arithmetic.
                    distances[start:end] = np.einsum('ij,ij->i', delta, delta, dtype=dtype)
        return distances

    def dot_candidates(self, indexes, query, size, count):
        """Conservative BLAS screen; None selects the direct-subtraction path.

        For exact squared distance D and the norm/dot screen S, use a common
        absolute bound E, not a relative error in D (which can cancel to zero).
        The float32 dot error is bounded by gamma_d * sum(abs(x_i*q_i));
        2*sum(abs(x_i*q_i)) <= ||x||^2 + ||q||^2. With eps32 twice unit
        roundoff, the factor 8 also covers norm reduction, final float64
        arithmetic, the scalar reference rounding, and flushed subnormals.
        If |S-D| <= E, every true TopK has S <= kth(S) + 2*E.
        """
        if self.norms is None or size < 128 or size <= count:
            return None
        np = self.np
        dimension = len(query)
        q = np.asarray(query, dtype=np.float32)
        with np.errstate(over='ignore', under='ignore', invalid='ignore'):
            if indexes is None:
                products = self.vectors @ q
            elif size * dimension * 4 <= 1024 * 1024:
                # Bounded gather is faster for selective filters. Above 1 MiB,
                # scan the existing matrix, then filter scores before TopK.
                products = self.vectors[indexes] @ q
            else:
                products = (self.vectors @ q)[indexes]
        if not np.isfinite(products).all():
            return None
        q_norm = math.fsum(value * value for value in query)
        distances = products.astype(np.float64)
        distances *= -2.0
        distances += self.norms if indexes is None else self.norms[indexes]
        distances += q_norm
        epsilon = (dimension + 4) * float(np.finfo(np.float32).eps)
        gamma = epsilon / (1 - epsilon)
        error = (8 * gamma * (self.max_norm + q_norm)
                 + 8 * dimension * float(np.finfo(np.float32).tiny))
        boundary = np.partition(distances, count-1)[count-1]
        candidates = np.flatnonzero(distances <= np.nextafter(boundary + 2*error, np.inf))
        # Norm cancellation or many ties can make this screen unselective.
        # Direct subtraction has a much tighter bound for such inputs.
        return candidates if len(candidates) <= max(4 * count, size // 10) else None

    def topk(self, conditions, query, count, include_vector=True):
        np = self.np
        indexes = self.candidate_indexes(conditions)
        size = len(self.rows) if indexes is None else len(indexes)
        if not size or count == 0:
            return []
        dimension = len(query)
        candidates = self.dot_candidates(indexes, query, size, count)
        # Both stored vectors and validated queries are float32. A bounded-error
        # screen is sufficient: only the final math.fsum distances determine K.
        single = dimension <= 4096
        if candidates is None:
            distances = self.squared_distances(indexes, query, size, np.float32 if single else np.float64)
        if candidates is None and single and not np.isfinite(distances).all():
            # Extreme finite inputs can overflow subtraction/squaring in float32.
            # Recompute this query in float64 before making any pruning decision.
            distances = self.squared_distances(indexes, query, size, np.float64)
            single = False
        # Include the entire boundary tie before primary-key ordering. Pure
        # argpartition[:K] would pick arbitrary IDs for equal-distance vectors.
        if candidates is None and size > count:
            boundary = np.partition(distances, count-1)[count-1]
            if single:
                # |screen - squared_distance| <= gamma * squared_distance + eta.
                # eps32 is twice unit roundoff; n+4 covers subtraction, square,
                # and any reduction order. eta also covers flushed subnormals.
                # For the Kth screen b, every true TopK has screen at most
                # b + 2*(gamma*b + eta)/(1-gamma). Keep that whole boundary.
                epsilon = (dimension + 4) * float(np.finfo(np.float32).eps)
                gamma = epsilon / (1 - epsilon)
                eta = 4 * dimension * float(np.finfo(np.float32).tiny)
                tolerance = 2 * (gamma * abs(boundary) + eta) / (1 - gamma)
            else:
                tolerance = abs(boundary) * np.finfo(np.float64).eps * max(1, dimension) * 16
            candidates = np.flatnonzero(distances <= np.nextafter(boundary + tolerance, np.inf))
        elif candidates is None:
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
