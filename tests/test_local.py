"""Учёт исходов генератором: отказ, ошибка, оборванный поток, недоступность и повторы как отдельные попытки."""
import io
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import loadgen  # noqa: E402

META = {'type': 'meta', 'pod': 'p1', 'version': 'A', 'warm': True, 'queue_seconds': 0.0}
CHUNK = {'type': 'chunk', 'i': 1, 'text': 'x', 'tokens': 1}
DONE = {'type': 'done', 'cache': 'miss', 'context_echo': []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        mode = self.path.strip('/')
        if mode in ('503', '429'):
            body = b'{"error":"x"}'
            self.send_response(int(mode))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.end_headers()
        lines = [META, CHUNK, CHUNK] + ([DONE] if mode == 'ok' else [])
        for line in lines:
            self.wfile.write((json.dumps(line) + '\n').encode())
            self.wfile.flush()
        self.close_connection = True


def closed_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class AccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def outcome(self, url):
        return loadgen.request_once(url, {'prompt': 'q'}, 'rid', timeout=5)

    def test_each_outcome_is_counted_separately(self):
        ok = self.outcome(self.base + '/ok')
        self.assertEqual((ok['outcome'], ok['chunks'], ok['pod']), ('ok', 2, 'p1'))
        self.assertIsNotNone(ok['ttfc_s'])
        broken = self.outcome(self.base + '/cut')
        self.assertEqual(broken['outcome'], 'aborted', 'поток без done не может считаться успехом')
        self.assertEqual(broken['chunks'], 2)
        self.assertEqual(self.outcome(self.base + '/503')['outcome'], 'http_5xx')
        self.assertEqual(self.outcome(self.base + '/429')['outcome'], 'rejected')
        self.assertEqual(self.outcome(f'http://127.0.0.1:{closed_port()}/x')['outcome'], 'connect_error')

    def test_retries_are_separate_attempts(self):
        out = io.StringIO()
        item = {'seq': 1, 'client': 0, 'session_id': 's', 'prefix_len': 10, 'max_new_tokens': 2, 'prompt': 'q',
                'phase': 'measure', 'planned_wall': None}
        loadgen.execute(self.base + '/cut', item, 5, 2, out, threading.Lock(), {'run': 't'})
        rows = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r['attempt'] for r in rows], [0, 1, 2])
        self.assertEqual({r['logical_id'] for r in rows}, {'t-1'})
        self.assertEqual(len({r['request_id'] for r in rows}), 3)
        self.assertTrue(all(r['outcome'] == 'aborted' for r in rows))
        out = io.StringIO()
        loadgen.execute(self.base + '/429', {**item, 'seq': 2}, 5, 2, out, threading.Lock(), {'run': 't'})
        self.assertEqual(len(out.getvalue().splitlines()), 1, '429 — отказ по ёмкости, без повтора')


if __name__ == '__main__':
    unittest.main(verbosity=2)
