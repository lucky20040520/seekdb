"""Run the checked-in file_sql_basic SQL body and expected result locally.

Only the SSH fixture setup/teardown includes are replaced with local fixture
creation. All SQL, mysqltest directives and expected output are unchanged.
Run on the same Linux host as a dedicated seekdb instance with secure_file_priv
allowing the temporary directory (the existing isolated instance uses '/').
"""

import argparse
import base64
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mysqltest', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[4]
    suite = repo / 'tools/deploy/mysql_test/test_suite/file_sql'
    case = (suite / 't/file_sql_basic.test').read_text(encoding='utf-8')
    for include in ('copy_file_sql_data.inc', 'remove_file_sql_data.inc'):
        directive = '--source mysql_test/test_suite/file_sql/include/' + include
        if case.count(directive) != 1:
            raise RuntimeError('unexpected fixture include: ' + include)
        case = case.replace(directive, '# Fixtures managed by local test driver')
    args.artifacts.mkdir(parents=True, exist_ok=True)
    artifacts = args.artifacts.resolve()
    with tempfile.TemporaryDirectory(prefix='seekdb-file-sql-mysqltest-') as directory:
        root = Path(directory)
        data = root / 'data_for_mysqltest_file_sql'
        data.mkdir()
        for source in (suite / 'data').iterdir():
            if source.suffix == '.b64':
                (data / source.stem).write_bytes(base64.b64decode(source.read_bytes(), validate=False))
            else:
                shutil.copyfile(source, data / source.name)
        test = artifacts / 'file_sql_basic.local.test'
        test.write_text('--disable_query_log\nlet $OBSERVER_DIR = ' + str(root) + ';\n' + case,
                        encoding='utf-8')
        command = [args.mysqltest, '--no-defaults', '--host=127.0.0.1',
                   '--port=' + str(args.port), '--user=root', '--protocol=tcp',
                   '--default-character-set=utf8mb4', '--disable-explain',
                   '--test-file=' + str(test), '--logdir=' + str(artifacts),
                   '--tmpdir=' + str(root),
                   '--result-file=' + str(suite / 'r/mysql/file_sql_basic.result')]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=120, cwd=repo / 'tools/deploy')
        (artifacts / 'mysqltest.log').write_text(result.stdout, encoding='utf-8')
        print(result.stdout, end='')
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
