"""Bounded-run external merge sort for SQL rows."""
import functools
import heapq
import json
import tempfile


def _compare(a, b, ordering, primary_key):
    for fn, descending in ordering:
        left, right = fn(a), fn(b)
        relation = (left > right) - (left < right)
        if relation:
            return -relation if descending else relation
    return (a[primary_key] > b[primary_key]) - (a[primary_key] < b[primary_key])


def sort_rows(rows, ordering, primary_key, directory, limit=None, run_bytes=1024 * 1024, fan_in=32):
    key = functools.cmp_to_key(lambda a, b: _compare(a, b, ordering, primary_key))
    files = []

    def make_run(batch):
        batch.sort(key=key)
        file = tempfile.TemporaryFile(dir=directory, mode="w+b")
        files.append(file)
        for row in batch:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        file.seek(0)
        return file

    def read_run(file):
        for line in file:
            yield json.loads(line)

    def merge(group):
        return heapq.merge(*(read_run(file) for file in group), key=key)

    def combine(group):
        target = tempfile.TemporaryFile(dir=directory, mode="w+b")
        files.append(target)
        for row in merge(group):
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        target.seek(0)
        for file in group:
            file.close()
            files.remove(file)
        return target

    try:
        batch, size, runs = [], 0, []
        for row in rows:
            encoded_size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if batch and size + encoded_size > run_bytes:
                runs.append(make_run(batch))
                if len(runs) >= fan_in * 2:
                    runs = [combine(runs[:fan_in])] + runs[fan_in:]
                batch, size = [], 0
            batch.append(row)
            size += encoded_size
        if batch:
            runs.append(make_run(batch))
        while len(runs) > fan_in:
            reduced = []
            for i in range(0, len(runs), fan_in):
                group = runs[i:i + fan_in]
                reduced.append(combine(group))
            runs = reduced
        for i, row in enumerate(merge(runs)):
            if limit is not None and i >= limit:
                break
            yield row
    finally:
        for file in files:
            file.close()
