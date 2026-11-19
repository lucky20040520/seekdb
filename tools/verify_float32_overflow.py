"""Distinguish rejected float32 input from overflow of legal arithmetic inputs."""
from contextlib import closing
import argparse
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import traceback
from unittest.mock import patch

import numpy as np

from minivector.engine import Database, DBError
from minivector.sql import execute
from minivector.vector import VectorCache


def printable(values):
    return [float(value) if math.isfinite(float(value)) else str(float(value)) for value in values]


def trial(root, budget, indexed, filtered=False):
    with closing(Database(root)) as db:
        execute(db, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2));')
        assert execute(db, 'INSERT INTO t VALUES (1,[3e38,0]),(2,[-3e38,0]),(3,[0,0]);') == 3
        stored = list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1])
        a = struct.unpack('<f', struct.pack('<f', 3e38))[0]
        assert stored == [[1, [a, 0.0]], [2, [-a, 0.0]], [3, [0.0, 0.0]]]
        try:
            execute(db, 'INSERT INTO t VALUES (4,[3.5e38,0]);')
        except DBError as error:
            input_error = {'code': error.code, 'message': str(error)}
        else:
            raise AssertionError('unrepresentable input accepted')
        assert input_error['code'] == 'TYPE'
        assert list(execute(db, 'SELECT id,v FROM t ORDER BY id;')[1]) == stored
        db.query_cache_limit = budget
        if indexed:
            execute(db, 'CREATE VECTOR INDEX vi ON t(v);')
        original = VectorCache.squared_distances
        calls = []
        scalar_terms = []
        scalar_stacks = []
        original_fsum = math.fsum

        def tracked_fsum(values):
            terms = list(values)
            scalar_terms.append(printable(terms))
            scalar_stacks.append([Path(frame.filename).name + ':' + frame.name + ':' + str(frame.lineno)
                                  for frame in traceback.extract_stack(limit=10)[:-1]
                                  if Path(frame.filename).name in ('engine.py', 'vector.py', 'flat_index.py', 'heapq.py')])
            return original_fsum(terms)

        def traced(cache, indexes, query, size, dtype):
            result = original(cache, indexes, query, size, dtype)
            calls.append({'compute_dtype': np.dtype(dtype).name, 'result_dtype': str(result.dtype),
                          'candidate_count': size, 'squared': printable(result)})
            return result

        with patch.object(VectorCache, 'squared_distances', traced), \
                patch.object(db, 'indexed_rows', wraps=db.indexed_rows) as disk, \
                patch.object(math, 'fsum', tracked_fsum):
            vector = '[0,0]' if filtered else '[3e38,0]'
            where = ' WHERE id=3' if filtered else ''
            columns, rows = execute(db, "SELECT id,L2_DISTANCE(v,'" + vector + "') AS d FROM t"
                                    + where + " ORDER BY d LIMIT 3;")
            actual = list(rows)
        query = [0.0, 0.0] if filtered else [a, 0.0]
        visible = [row for row in stored if row[0] == 3] if filtered else stored
        truth = sorted([[key, math.sqrt(math.fsum((x-y)**2 for x,y in zip(vector, query)))]
                        for key,vector in visible], key=lambda row: (row[1], row[0]))
        assert columns == ['id', 'd'] and actual == truth
        assert [row[0] for row in actual] == ([3] if filtered else [1, 3, 2])
        assert all(math.isfinite(row[1]) for row in actual)
        if budget:
            assert [c['compute_dtype'] for c in calls] == (['float32'] if filtered else ['float32', 'float64'])
            assert calls[0]['squared'] == ([0.0] if filtered else [0.0, 'inf', 'inf'])
            assert calls[0]['candidate_count'] == (1 if filtered else 3)
        else:
            assert not calls
        assert disk.call_count == int(indexed)
        if filtered:
            assert scalar_terms and all(t == [0.0, 0.0] for t in scalar_terms)
        vectors = np.asarray([row[1] for row in visible], dtype=np.float32)
        with np.errstate(over='ignore', invalid='ignore'):
            delta = np.subtract(vectors, np.asarray(query, dtype=np.float32), dtype=np.float32)
            square = np.multiply(delta, delta, dtype=np.float32)
        return {'budget': budget, 'indexed': indexed, 'stored_a': a, 'input_3_5e38_error': input_error,
                'float32_delta_first_component': printable(delta[:, 0]),
                'float32_squared_first_component': printable(square[:, 0]),
                'screen_calls': calls, 'rows': actual, 'disk_index_calls': disk.call_count,
                'filter': 'id=3' if filtered else None, 'scalar_fsum_terms': scalar_terms,
                'scalar_fsum_stacks': scalar_stacks,
                'independent_truth_exact_match': actual == truth}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('initial', 'filtered'), default='initial', nargs='?')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'MINIVECTOR_NO_NUMPY': '0'}):
        for budget,indexed in ((16*1024*1024, False), (0, False), (0, True)):
            print(json.dumps(trial(Path(root) / (str(budget)+'-'+str(indexed)), budget, indexed,
                                   args.stage == 'filtered'), allow_nan=False))
