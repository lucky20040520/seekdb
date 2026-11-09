"""Read-only public assertion runner over the core API, NOT official wire driver."""
import argparse
import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minivector.engine import Database, DBError
from minivector.sql import execute


def same_value(actual, expected):
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            same_value(a, b) for a, b in zip(actual, expected))
    if type(expected) is int:
        return type(actual) is int and actual == expected
    if type(expected) is float:
        return type(actual) in (int, float) and math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-5)
    return type(actual) is type(expected) and actual == expected


def check(actual, expected):
    if 'error' in expected:
        assert actual.get('error'), 'expected failure with a nonempty error code'
        return
    assert 'error' not in actual, 'unexpected error: ' + str(actual)
    for field in ('affected_rows', 'columns'):
        if field in expected:
            assert actual.get(field) == expected[field], field + ' mismatch'
    if 'rows' not in expected:
        return
    rows = actual.get('rows')
    assert isinstance(rows, list), 'missing query rows'
    if expected.get('unordered'):
        remaining = list(rows)
        for row in expected['rows']:
            index = next((i for i, candidate in enumerate(remaining) if same_value(candidate, row)), None)
            assert index is not None, 'unordered row missing'
            remaining.pop(index)
        assert not remaining, 'unexpected duplicate or extra rows'
    else:
        assert same_value(rows, expected['rows']), 'ordered rows mismatch'


def run(package):
    manifest = json.loads((package/'manifest.json').read_text(encoding='utf-8'))
    results, total = [], 0
    with tempfile.TemporaryDirectory(prefix='minivector-public-') as root:
        for case in manifest['cases']:
            data = Path(root)/case['id']
            db = Database(data)
            mode, index, errors = 'exact', 0, []
            sql_path, result_path = package/case['test'], package/case['result']
            expected = json.loads(result_path.read_text(encoding='utf-8'))
            try:
                for line_number, line in enumerate(sql_path.read_text(encoding='utf-8').splitlines(), 1):
                    line = line.strip()
                    if not line:
                        continue
                    if line == '--restart':
                        db.close()
                        db = Database(data)
                        continue
                    if line in ('--mode ann', '--mode exact'):
                        mode = line.split()[-1]
                        continue
                    if line.startswith('--'):
                        raise ValueError('unknown public directive: '+line)
                    try:
                        value = execute(db, line, mode=mode)
                        if isinstance(value, tuple):
                            columns, rows = value
                            actual = {'columns': columns, 'rows': list(rows)}
                        else:
                            actual = {'affected_rows': value if value is not None else 0}
                    except DBError as error:
                        actual = {'error': error.code, 'message': str(error)}
                    try:
                        assert index < len(expected), 'missing expected assertion'
                        check(actual, expected[index])
                    except AssertionError as error:
                        errors.append({'statement': index+1, 'line': line_number,
                                       'reason': str(error), 'actual': actual,
                                       'expected': expected[index] if index < len(expected) else None})
                    index += 1
                if index != len(expected) or index != case['statements']:
                    errors.append({'reason': 'statement/assertion/manifest count mismatch'})
            finally:
                db.close()
            total += index
            record = {'id': case['id'], 'statements': index, 'passed': not errors, 'failures': errors}
            results.append(record)
            print(('PASS ' if not errors else 'FAIL ')+case['id']+' (%d SQL)'%index, flush=True)
    hashes = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(package.rglob('*')) if p.is_file()}
    return {'package_version': manifest['version'], 'cases': len(results), 'statements': total,
            'passed_cases': sum(r['passed'] for r in results), 'results': results,
            'package_sha256': hashes,
            'scope': 'Public fixtures unchanged; core API runner, not official process communication or formal admission',
            'error_policy': 'Nonempty code minimum from public README; frozen official error mapping not available'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[1]/'mysqltest')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run(args.package.resolve())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('%d/%d cases, %d SQL'%(result['passed_cases'],result['cases'],result['statements']))
    return 0 if result['passed_cases'] == result['cases'] else 1


if __name__ == '__main__':
    sys.exit(main())
