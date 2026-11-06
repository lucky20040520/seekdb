#!/usr/bin/env python3
"""Capture complete tokenizer results and known long-IK errors on a dedicated DB."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mysql', default=(
        'mysql -h127.0.0.1 -P2881 -uroot -N -s -A --batch --raw --force '
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
    allowed_lines = {
        i for i, line in enumerate(fixture.read_text(encoding='utf-8').splitlines(), 1)
        if re.match(r"^SELECT TOKENIZE\(CAST\(body AS CHAR\), 'ik', .* WHERE id=(10|11|12);$", line)
    }
    errors = []
    for line in result.stderr.splitlines():
        match = re.match(rb'^ERROR ([0-9]+) .* at line ([0-9]+):', line)
        if not match or match[1] != b'1210' or int(match[2]) not in allowed_lines:
            raise RuntimeError('Unexpected SQL error; inspect results.stderr')
        errors.append(match)
    summary = {
        'stdout_sha256': hashlib.sha256(result.stdout).hexdigest(),
        'stderr_sha256': hashlib.sha256(result.stderr).hexdigest(),
        'known_long_ik_errors': len(errors),
        'reference_matches': args.reference is not None,
    }
    (args.output_directory / 'summary.json').write_bytes(
        (json.dumps(summary, indent=2) + '\n').encode())
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
