"""Исправленный HTTP-прототип на стандартной библиотеке. Только localhost, STUB ONLY.

Целевая версия этого контракта — app/api.py на FastAPI.
"""
import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import release
from api_contract import StubBackend, generation_limit, validate_generate


class ServiceState:
    def __init__(self, root, warmup_seconds):
        self.root = root
        # Снимок на момент загрузки: смена accepted.json не меняет модель в памяти.
        self.manifest = json.loads((root / 'accepted.json').read_text(encoding='utf-8'))
        release.verify_artifact(self.manifest, root)
        self.backend = StubBackend(self.manifest)
        self.limit = generation_limit(self.manifest)
        self.compute = threading.Semaphore(1)
        self.warmup_finished = threading.Event()

        def warmup():
            time.sleep(warmup_seconds)
            self.warmup_finished.set()
        threading.Thread(target=warmup, daemon=True).start()

    def generate(self, prompt, tokens):
        started = time.perf_counter()
        with self.compute:
            acquired = time.perf_counter()
            result = self.backend.generate(prompt, tokens)
        finished = time.perf_counter()
        result.update(model_id=self.backend.loaded_id, runtime_sha256=self.backend.runtime_sha256,
                      request_id=str(uuid.uuid4()), queue_seconds=acquired - started,
                      generation_seconds=finished - acquired)
        return result


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            ready = state.warmup_finished.is_set()
            if self.path == '/health':
                self.send_json(200, {'alive': True, 'backend': 'STUB ONLY'})
            elif self.path == '/ready':
                self.send_json(200 if ready else 503, {'ready': ready})
            elif self.path == '/debug':
                self.send_json(200, {'loaded_id': state.backend.loaded_id, 'calls': state.backend.calls,
                                     'warmup_finished': ready})
            else:
                self.send_json(404, {'error': 'not found'})

        def do_POST(self):
            if self.path != '/generate':
                self.send_json(404, {'error': 'not found'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 65536:
                    self.send_json(413, {'error': 'body too large'})
                    return
                prompt, tokens = validate_generate(json.loads(self.rfile.read(size)), state.limit)
            except ValueError as error:
                self.send_json(400, {'error': str(error)})
                return
            if not state.warmup_finished.is_set():
                self.send_json(503, {'error': 'модель ещё не готова'})
                return
            self.send_json(200, state.generate(prompt, tokens))
    return Handler


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workspace', type=Path, default=Path('.lab'))
    p.add_argument('--port', type=int, default=8091)
    p.add_argument('--warmup-seconds', type=float, default=5)
    args = p.parse_args()
    state = ServiceState(args.workspace, args.warmup_seconds)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(state))
    print(f'STUB ONLY: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
