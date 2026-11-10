"""Disk-backed random-hyperplane candidate index; exact distances rerank hits."""
import heapq
import math
import random
import struct
import tempfile


class ProjectionIndex:
    BITS = 8
    _KEY = struct.Struct("<q")

    def __init__(self, db, table, column, dimension):
        self.db, self.table, self.column = db, table, column
        rng = random.Random(18037 + dimension)
        self.planes = [[rng.choice((-1.0, 1.0)) for _ in range(dimension)] for _ in range(self.BITS)]
        self.buckets = {}
        self.count = 0
        primary_key = db.schema(table)["primary_key"]
        try:
            for row in db.rows(table):
                bucket = self.signature(row[column])
                if bucket not in self.buckets:
                    self.buckets[bucket] = tempfile.TemporaryFile(dir=db.directory, mode="w+b")
                self.buckets[bucket].write(self._KEY.pack(row[primary_key]))
                self.count += 1
        except BaseException:
            self.close()
            raise

    def signature(self, vector):
        signature = 0
        for bit, plane in enumerate(self.planes):
            if sum(a * b for a, b in zip(vector, plane)) >= 0:
                signature |= 1 << bit
        return signature

    def probe_order(self, vector):
        # Crossing a nearby hyperplane is more plausible than crossing a
        # distant one. Rank multiprobe buckets by their boundary margins.
        projections = [sum(a * b for a, b in zip(vector, plane)) for plane in self.planes]
        signature = sum(1 << bit for bit, value in enumerate(projections) if value >= 0)
        costs = [0.0] * (1 << self.BITS)
        for mask in range(1, len(costs)):
            bit = (mask & -mask).bit_length() - 1
            costs[mask] = costs[mask & (mask - 1)] + abs(projections[bit])
        return sorted(range(len(costs)), key=lambda bucket: (costs[bucket ^ signature], bucket))

    def search(self, query, limit, predicate, candidate_budget=None):
        if limit == 0:
            return []
        candidates = []
        remaining = candidate_budget
        buckets = self.probe_order(query) if remaining is not None else range(1 << self.BITS)
        for bucket in buckets:
            file = self.buckets.get(bucket)
            if file is None:
                continue
            file.seek(0)
            while True:
                data = file.read(self._KEY.size)
                if not data:
                    break
                if len(data) != self._KEY.size:
                    raise ValueError("truncated vector index")
                key = self._KEY.unpack(data)[0]
                row = self.db.get(self.table, key)
                if row is not None and predicate(row):
                    distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(row[self.column], query)))
                    # Keep only the best K candidates in memory.
                    entry = (-distance, -key, row)
                    if len(candidates) < limit:
                        heapq.heappush(candidates, entry)
                    elif entry[:2] > candidates[0][:2]:
                        heapq.heapreplace(candidates, entry)
            if remaining is not None:
                remaining -= file.tell() // self._KEY.size
            if remaining is not None and remaining <= 0 and len(candidates) >= limit:
                break
        return sorted(((-distance, -key, row) for distance, key, row in candidates), key=lambda result: (result[0], result[1]))

    def close(self):
        for file in self.buckets.values():
            file.close()
        self.buckets.clear()
