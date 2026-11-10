"""Exercise documented examples in fresh offline containers from delivered source."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import uuid

from benchmark_stdio import docker
from run_stdio_public import Session
from minivector.__main__ import statements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    assert docker('image', 'inspect', '--format', '{{.Architecture}}', args.image).strip() == 'amd64'
    versions = json.loads(docker('run', '--rm', '--network', 'none', '--entrypoint', 'python',
        args.image, '-c', 'import sys,json,numpy;print(json.dumps([sys.version.split()[0],numpy.__version__]))'))
    assert versions == ['3.12.8', '1.26.4'], 'delivered dependency versions differ'
    token = 'minivector-delivery-' + uuid.uuid4().hex
    volume, names = token + '-data', [token + '-first', token + '-restart']
    docker('volume', 'create', volume)
    session = None
    try:
        for index, file in enumerate(('first-run.sql', 'after-restart.sql')):
            command = ['docker', 'run', '--rm', '-i', '--name', names[index], '--network', 'none',
                       '--mount', 'type=volume,src=' + volume + ',dst=/data', args.image, '--protocol', 'diagnostic']
            session = Session(None, 'exact', 30, command=command)
            text = (Path(__file__).resolve().parents[1]/'examples'/file).read_text(encoding='utf-8')
            results = [session.execute(sql) for sql in statements(io.StringIO(text))]
            if index:
                assert results == [(['id','title','embedding'], [[1,'alpha',[3.0,4.0]]]),
                                   (['id','distance'], [[1,5.0]]), (['id'], [])]
            changed = docker('diff', names[index]).splitlines()
            assert all(line.split(' ', 1)[1].startswith('/data') for line in changed), changed
            for path in ('/proc/net/tcp', '/proc/net/tcp6'):
                assert len(docker('exec', names[index], 'cat', path).splitlines()) == 1, 'unexpected network socket'
            session.close()
            session = None
        print(json.dumps({'status':'PASS','architecture':'amd64','versions':versions,
              'verified':'empty volume,documented examples,EOF rollback,restart persistence,no network sockets,no writes outside data volume'}))
    finally:
        if session is not None:
            session.abort()
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
        docker('volume', 'rm', volume)


if __name__ == '__main__':
    main()
