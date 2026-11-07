#!/usr/bin/env python3
"""Compare complete tokenizer and index results on a dedicated database."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mysql', default=(
        'mysql -h127.0.0.1 -P2881 -uroot -N -s -A --batch --raw '
        '--default-character-set=utf8mb4'))
    parser.add_argument('--output-directory', type=Path, required=True)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    if args.output_directory.exists():
        parser.error('Use a fresh output directory.')
    args.output_directory.mkdir(parents=True)
    fixture = Path(__file__).with_name('fts_pipeline_regression.sql')
    result = subprocess.run(shlex.split(args.mysql), input=fixture.read_bytes(), capture_output=True)
    for name, data in [('results.out', result.stdout), ('results.stderr', result.stderr)]:
        (args.output_directory / name).write_bytes(data)
        if args.reference is not None and data != (args.reference / name).read_bytes():
            raise RuntimeError('Full output differs from reference: ' + name)
    if result.returncode:
        raise RuntimeError('MySQL client failed: ' + str(result.returncode))
    if result.stderr:
        raise RuntimeError('Unexpected SQL stderr; inspect results.stderr')
    summary = {
        'stdout_sha256': hashlib.sha256(result.stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(result.stderr).hexdigest(),
        'sql_error_count': 0,
        'reference_matches': args.reference is not None,
    }
    (args.output_directory / 'summary.json').write_bytes(
        (json.dumps(summary, indent=2) + '\n').encode())
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
