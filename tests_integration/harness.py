"""Общий адаптер запуска: одни и те же тесты идут на прототипе и на Prefect/FastAPI.

HW_LAB_CMD и HW_SERVICE_CMD задают команду, например
HW_LAB_CMD="-m flows.release_flow" HW_SERVICE_CMD="-m app.api".
"""
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB_CMD = shlex.split(os.environ.get('HW_LAB_CMD', 'lab.py'))
SERVICE_CMD = shlex.split(os.environ.get('HW_SERVICE_CMD', 'service.py'))
ENV = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONPATH': str(ROOT)}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def http(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'{}')


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / 'lab'
        self.assertEqual(self.lab('init').returncode, 0)

    def lab(self, *args):
        result = subprocess.run([sys.executable, *LAB_CMD, '--workspace', str(self.ws), *args], cwd=ROOT, env=ENV,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
        print(f'$ lab {" ".join(args)} -> exit {result.returncode}\n{result.stdout}{result.stderr[-400:]}')
        return result

    def publish_in_other_process(self, version):
        code = ('import json,sys,pathlib,release\n'
                'r=pathlib.Path(sys.argv[1]);v=sys.argv[2]\n'
                'load=lambda n:json.loads((r/n).read_text(encoding="utf-8"))\n'
                'release.publish(load(f"manifest-{v}.json"),load(f"report-{v}.json"),r/"accepted.json")')
        subprocess.run([sys.executable, '-c', code, str(self.ws), version], cwd=ROOT, env=ENV, check=True, timeout=60)

    def events(self, model_id=None):
        path = self.ws / 'publications.jsonl'
        rows = [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()] if path.is_file() else []
        return [r for r in rows if model_id is None or r['model_id'] == model_id]

    def start_service(self, warmup=0.0, wait_health=True):
        port = free_port()
        process = subprocess.Popen([sys.executable, *SERVICE_CMD, '--workspace', str(self.ws), '--port', str(port),
                                    '--warmup-seconds', str(warmup)], cwd=ROOT, env=ENV,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_service, process)
        base = f'http://127.0.0.1:{port}'
        if wait_health:
            self.wait_for(lambda: http('GET', base + '/health')[0] == 200, process)
        return process, base

    def stop_service(self, process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)

    def wait_for(self, predicate, process=None, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                self.fail(f'процесс завершился с кодом {process.returncode}')
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.1)
        self.fail('условие не выполнилось вовремя')
