#!/usr/bin/env python3
# Copyright (c) 2025 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compare full MATCH results and assert score-usage plans on a dedicated DB."""
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
    parser.add_argument('--expect-scored-membership', action='store_true',
                        help='Use for the original binary, which scores COUNT(*) MATCH.')
    args = parser.parse_args()
    if args.output_directory.exists():
        parser.error('Use a new output directory; existing evidence is never overwritten.')
    args.output_directory.mkdir(parents=True)
    mysql = shlex.split(args.mysql)
    fixture = Path(__file__).with_name('fts_score_usage.sql')
    result = subprocess.run(mysql, input=fixture.read_bytes(), capture_output=True)
    (args.output_directory / 'results.out').write_bytes(result.stdout)
    (args.output_directory / 'results.stderr').write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError('SQL regression failed; see results.stderr')
    if args.reference is not None and result.stdout != args.reference.read_bytes():
        raise RuntimeError('Complete SQL output differs from the reference')
    plans = {
        'membership': "SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('database audit')",
        'projection': "SELECT id, MATCH(body) AGAINST('database audit') FROM docs WHERE MATCH(body) AGAINST('database audit')",
        'numeric': "SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') > 0.1",
        'order': "SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') ORDER BY MATCH(body) AGAINST('database audit') DESC LIMIT 2",
        'boolean': "SELECT id FROM docs WHERE MATCH(body) AGAINST('+database -audit' IN BOOLEAN MODE)",
    }
    checks = {}
    for name, sql in plans.items():
        result = subprocess.run(mysql + ['-D', 'codex_fts_score_usage', '-e',
                                         'EXPLAIN EXTENDED ' + sql], capture_output=True, check=True)
        (args.output_directory / (name + '.plan.txt')).write_bytes(result.stdout)
        text = result.stdout.decode('utf-8')
        expected = 'false' if name == 'membership' and not args.expect_scored_membership else 'true'
        checks[name] = ('calc_relevance=' + expected in text
                        or (name == 'numeric' and 'has_functional_lookup=true' in text))
    summary = {'sql_sha256': hashlib.sha256((args.output_directory / 'results.out').read_bytes()).hexdigest(),
               'reference_matches': args.reference is not None, 'plan_checks': checks}
    (args.output_directory / 'summary.json').write_bytes(
        (json.dumps(summary, indent=2) + '\n').encode('utf-8'))
    if not all(checks.values()):
        raise RuntimeError('Execution-plan assertions failed: ' + str(checks))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
