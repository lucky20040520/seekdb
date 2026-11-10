"""Current official STDIO_PROTOCOL.md adapter; engine remains independently implemented."""
import json
import math
import shutil
import sys
import tempfile

from .engine import DBError
from .sql import execute


def strict_json(line):
    def invalid(value):
        raise ValueError('nonfinite JSON number')
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite JSON number')
        return result
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(line, parse_constant=invalid, parse_float=finite, object_pairs_hook=pairs)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')), flush=True)


def emit_query(db, ident, columns, rows):
    # Build the single physical JSON line before sending any bytes: a lazy
    # query error cannot leave half a response. Large results spill to /data,
    # rather than accumulating all rows in process memory.
    with tempfile.SpooledTemporaryFile(max_size=256*1024, mode='w+',
                                      encoding='utf-8', newline='\n', dir=str(db.path)) as output:
        header = json.dumps({'id': ident, 'ok': True, 'columns': columns},
                            ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        output.write(header[:-1] + ',"rows":[')
        first = True
        for row in rows:
            if not first:
                output.write(',')
            json.dump(row, output, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            first = False
        output.write(']}\n')
        output.seek(0)
        shutil.copyfileobj(output, sys.stdout, length=65536)
        sys.stdout.flush()


def serve(db):
    try:
        emit({'type': 'ready'})
        for line in sys.stdin:
            ident = None
            try:
                request = strict_json(line)
                if not isinstance(request, dict) or 'id' not in request:
                    raise ValueError('request object with id required')
                ident = request['id']
                if request.get('op') == 'shutdown':
                    db.close()
                    emit({'id': ident, 'ok': True})
                    return
                if request.get('op') != 'execute':
                    raise DBError('UNSUPPORTED_OP', 'unsupported operation')
                sql, mode = request.get('sql'), request.get('search_mode', 'exact')
                if not isinstance(sql, str) or mode not in ('exact', 'ann'):
                    raise ValueError('SQL string and exact/ann search_mode required')
                result = execute(db, sql, mode=mode)
                if isinstance(result, tuple):
                    emit_query(db, ident, *result)
                else:
                    emit({'id': ident, 'ok': True,
                          'affected_rows': result if result is not None else 0})
            except DBError as error:
                emit({'id': ident, 'ok': False, 'error': {'code': error.code, 'message': str(error)}})
            except (ValueError, TypeError, OverflowError) as error:
                emit({'id': ident, 'ok': False, 'error': {'code': 'PROTOCOL', 'message': str(error)}})
    finally:
        db.close()
