"""Run the existing paired benchmark from Windows with Docker Desktop/WSL2.

The host owns Docker orchestration. No container receives the Docker socket or
writable repository access. Only fresh benchmark cgroups and volumes are touched.
Use the same arguments as performance_checker.paired; requires docker-desktop.
"""
import json
import os
import re
import subprocess
import time
import uuid

from performance_checker import checker, paired
from performance_checker.resources import parse_io_stat, parse_linux_device_number, parse_memory_peak


def wsl(script, *args):
    result = subprocess.run(
        ['wsl.exe', '-d', 'docker-desktop', '-u', 'root', '--', 'sh', '-s', '--', *args],
        input=script.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-2000:])
    return result.stdout.decode('utf-8')


class DesktopMeasurement:
    def __init__(self, args, volume):
        if not re.fullmatch(r'perfcheck-data-[0-9a-f]{32}', volume):
            raise ValueError('unexpected benchmark volume')
        self.args = args
        self.name = 'minivector-measure-' + uuid.uuid4().hex
        self.path = '/sys/fs/cgroup/' + self.name
        info = json.loads(checker.docker('volume', 'inspect', volume).stdout)[0]
        if info['Mountpoint'] != '/var/lib/docker/volumes/' + volume + '/_data':
            raise ValueError('unexpected Docker volume mountpoint')
        self.data = '/mnt/docker-desktop-disk/data/docker/volumes/' + volume + '/_data'
        dev = wsl('set -eu\nstat -c %d "$1"\n', self.data)
        self.device = parse_linux_device_number(dev)
        if args.io_device != self.device:
            raise ValueError('explicit I/O device differs from the benchmark volume')
        wsl('set -eu\nmkdir "$1"\n'
            'if ! printf "+cpu +memory +io +pids" > "$1/cgroup.subtree_control"; then\n'
            '  rmdir "$1"; exit 1\nfi\n', self.path)

    def finish(self):
        raw = {'collector': 'external-parent-cgroup-v2-via-wsl', 'cgroup': self.name,
               'data_device': self.device, 'errors': [], 'sync_exit_code': None}
        io_bytes = memory_bytes = None
        names = ('io.stat', 'memory.peak', 'memory.stat', 'memory.events', 'cgroup.events', 'cpu.stat')
        try:
            # BusyBox sync -f calls syncfs outside the measured parent cgroup.
            wsl('set -eu\nsync -f "$1"\n', self.data)
            raw['sync_exit_code'] = 0
            prior, stable = None, 0
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                output = wsl('set -eu\nfor item in io.stat memory.peak memory.stat memory.events cgroup.events cpu.stat; do\n'
                             'cat "$1/$item"\nprintf "\\036"\ndone\n', self.path)
                parts = output.split('\x1e')
                if len(parts) != len(names) + 1:
                    raise ValueError('incomplete cgroup snapshot')
                snapshot = dict(zip(names, parts[:-1]))
                mem = dict(line.split() for line in snapshot['memory.stat'].splitlines())
                events = dict(line.split() for line in snapshot['cgroup.events'].splitlines())
                if events.get('populated') != '0':
                    raise RuntimeError('database processes must exit before measurement finishes')
                clean = mem.get('file_dirty') == '0' and mem.get('file_writeback') == '0'
                stable = stable + 1 if clean and snapshot['io.stat'] == prior else 0
                prior = snapshot['io.stat']
                if stable >= 3:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('writeback or I/O counters did not settle')
            raw.update(snapshot)
            raw['settled_samples'] = stable
            sizes = wsl('set -eu\nfind "$1" -maxdepth 1 -type f -exec stat -c %s {} \\;\n', self.data)
            sizes = list(map(int, sizes.splitlines()))
            raw['data_files'], raw['logical_data_bytes'] = len(sizes), sum(sizes)
            memory_bytes = parse_memory_peak(snapshot['memory.peak'])
            memory_events = dict(line.split() for line in snapshot['memory.events'].splitlines())
            if any(int(memory_events.get(key, '0')) for key in ('oom', 'oom_kill', 'max')):
                raise RuntimeError('memory resource limit reached')
            values = parse_io_stat(snapshot['io.stat'])[self.device]
            io_bytes = values['rbytes'] + values['wbytes']
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raw['errors'].append(str(exc))
        return {'io_bytes': io_bytes, 'memory_bytes': memory_bytes, 'raw': raw}

    def cleanup(self):
        wsl('set -eu\nfor attempt in 1 2 3 4 5 6 7 8 9 10; do\n'
            'if rmdir "$1" 2>/dev/null; then exit 0; fi\nsleep 0.1\ndone\nrmdir "$1"\n', self.path)


if __name__ == '__main__':
    if os.name != 'nt':
        raise SystemExit('Use performance_checker.paired on native Linux.')
    checker.Measurement = DesktopMeasurement
    paired.main()
