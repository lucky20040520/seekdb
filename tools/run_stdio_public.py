"""Exercise public assertions through OUR diagnostic pipe protocol, not official wire.

Replay every case separately in each startup mode. Public --mode directives are
not sent as SQL: both full replays cover the requested mode's assertions, without
claiming that an official live mode-switch protocol has been implemented.
"""
import argparse
from collections import deque
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

from run_public import run
from minivector.engine import DBError


class Session:
    def __init__(self, data, mode, timeout, command=None):
        self.timeout = timeout
        self.lines = queue.Queue()
        self.errors = deque(maxlen=40)
        self.process = subprocess.Popen(
            command or [sys.executable, '-u', '-m', 'minivector', '--data-dir', str(data),
                        '--mode', mode, '--protocol', 'diagnostic'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1)
        def stdout():
            try:
                for line in self.process.stdout:
                    self.lines.put(line.rstrip('\n'))
            finally:
                self.lines.put(None)
        def stderr():
            for line in self.process.stderr:
                self.errors.append(line.rstrip('\n'))
        self.threads = [threading.Thread(target=stdout, daemon=True),
                        threading.Thread(target=stderr, daemon=True)]
        for thread in self.threads:
            thread.start()
        try:
            if self.read() != 'LOCAL_READY':
                raise AssertionError('unexpected diagnostic readiness')
        except BaseException:
            self.abort()
            raise

    def read(self):
        try:
            line = self.lines.get(timeout=self.timeout)
        except queue.Empty:
            raise AssertionError('diagnostic pipe timeout; stderr=' + repr(list(self.errors)))
        if line is None:
            raise AssertionError('unexpected process EOF; stderr=' + repr(list(self.errors)))
        return line

    def execute(self, sql):
        # Deliberately no newline: test that flushed semicolon is sufficient.
        self.process.stdin.write(sql)
        self.process.stdin.flush()
        first = self.read()
        if first.startswith('ERROR '):
            _, code, message = first.split(' ', 2)
            raise DBError(code, message)
        if first.startswith('OK '):
            return int(first[3:])
        columns, rows = first.split('\t'), []
        while True:
            line = self.read()
            if line == 'END':
                break
            row = [json.loads(cell) for cell in line.split('\t')]
            if len(row) != len(columns):
                raise AssertionError('diagnostic column count mismatch')
            rows.append(row)
        return columns, rows

    def abort(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=self.timeout)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
        for thread in self.threads:
            thread.join(timeout=self.timeout)

    def close(self):
        try:
            self.process.stdin.close()
            if self.process.wait(timeout=self.timeout) != 0:
                raise AssertionError('nonzero normal shutdown; stderr=' + repr(list(self.errors)))
            for thread in self.threads:
                thread.join(timeout=self.timeout)
            if not self.lines.empty() and self.lines.get_nowait() is not None:
                raise AssertionError('unexpected trailing stdout')
        finally:
            self.abort()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[1]/'mysqltest')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=20)
    args = parser.parse_args()
    reports = []
    for mode in ('exact', 'ann'):
        print('DIAGNOSTIC STARTUP MODE ' + mode, flush=True)
        report = run(args.package.resolve(),
                     session_factory=lambda data: Session(data, mode, args.timeout))
        report.update(scope='Own diagnostic subprocess pipe, not official wire or live mode switch',
                      startup_mode=mode)
        reports.append(report)
    result = {'reports': reports, 'mode_policy': 'Two complete replays in fixed startup modes; public mode directives not implemented as live wire commands'}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    passed = sum(report['passed_cases'] for report in reports)
    print('%d/40 case replays, 190 SQL over diagnostic pipes' % passed, flush=True)
    return 0 if passed == 40 else 1


if __name__ == '__main__':
    sys.exit(main())
