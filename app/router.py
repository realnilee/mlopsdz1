"""Cache-aware маршрутизатор с учётом очереди (третья политика опыта B).

Для каждого Ready Pod оценивает время до первой части:
    оценка = ожидание слота + prefill,
    ожидание = max(0, в работе + 1 − C) / C × S,  prefill = hit_cost, если префикс есть в индексе Pod, иначе miss_cost.
C — параллельность Pod, S — сглаженная длительность ответа этого Pod (измеряется здесь же).
Индекс префиксов — приближение: LRU того же размера, что кеш Pod, обновляется по факту отправки.
Pod с префиксом выбирается, только пока его очередь не съела выгоду от кеша; иначе — свободный Pod.
Стоимости hit/miss берутся из конфигурации имитатора; на настоящем engine их нужно измерять.
Список Pod — DNS headless Service: в нём только Ready-адреса.
"""
import hashlib
import http.client
import json
import os
import random
import socket
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKENDS = os.environ.get('BACKENDS_HOST', 'sim-headless')
PORT = int(os.environ.get('BACKEND_PORT', 8000))
C = int(os.environ.get('MAX_CONCURRENCY', 2))
HIT, MISS = float(os.environ.get('CACHE_HIT_COST', 0.25)), float(os.environ.get('CACHE_MISS_COST', 0.6))
INDEX_SIZE = int(os.environ.get('PREFIX_CACHE_SIZE', 16))
MAX_INFLIGHT = int(os.environ.get('ROUTER_MAX_INFLIGHT_PER_POD', 18))
NAME = os.environ.get('POD_NAME', socket.gethostname())


class Backend:
    def __init__(self, ip):
        self.ip, self.inflight, self.service = ip, 0, 0.7
        self.index = OrderedDict()

    def cached(self, key):
        return key in self.index

    def remember(self, key):
        self.index[key] = True
        self.index.move_to_end(key)
        while len(self.index) > INDEX_SIZE:
            self.index.popitem(last=False)

    def estimate(self, key):
        wait = max(0, self.inflight + 1 - C) / C * self.service
        return wait + (HIT if self.cached(key) else MISS)


class Pool:
    def __init__(self):
        self.lock = threading.Lock()
        self.backends = {}
        threading.Thread(target=self.refresh, daemon=True).start()

    def refresh(self):
        while True:
            try:
                ips = {a[4][0] for a in socket.getaddrinfo(BACKENDS, PORT, proto=socket.IPPROTO_TCP)}
            except OSError:
                ips = set()
            with self.lock:
                for ip in ips - set(self.backends):
                    self.backends[ip] = Backend(ip)
                for ip in set(self.backends) - ips:
                    del self.backends[ip]
            time.sleep(1)

    def choose(self, key):
        with self.lock:
            live = [b for b in self.backends.values() if b.inflight < MAX_INFLIGHT]
            if not live:
                return None, {}
            scores = {b.ip: round(b.estimate(key), 3) for b in live}
            best = min(scores.values())
            pick = random.choice([b for b in live if abs(scores[b.ip] - best) < 1e-9])
            pick.inflight += 1
            pick.remember(key)
            return pick, scores

    def done(self, backend, seconds):
        with self.lock:
            backend.inflight -= 1
            if seconds is not None:
                backend.service = 0.8 * backend.service + 0.2 * seconds


POOL = Pool()


def prefix_key(payload):
    prefix = payload.get('prefix', '') if isinstance(payload, dict) else ''
    history = payload.get('history', []) if isinstance(payload, dict) else []
    text = (prefix if isinstance(prefix, str) else '') + '\x00' + '\x00'.join(h for h in history if isinstance(h, str))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, payload, extra=None):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ('/health', '/ready'):
            with POOL.lock:
                n = len(POOL.backends)
            self.reply(200 if n or self.path == '/health' else 503, {'backends': n, 'router': NAME})
        else:
            self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/generate':
            self.reply(404, {'error': 'not found'})
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        rid = self.headers.get('X-Request-ID') or str(uuid.uuid4())
        try:
            key = prefix_key(json.loads(body))
        except ValueError:
            key = 'invalid'
        backend, scores = POOL.choose(key)
        if backend is None:
            print(json.dumps({'event': 'route', 'request_id': rid, 'outcome': 'rejected_by_router'}), flush=True)
            self.reply(429, {'error': 'все реплики на пределе', 'request_id': rid})
            return
        started = time.monotonic()
        finished = None
        try:
            conn = http.client.HTTPConnection(backend.ip, PORT, timeout=120)
            conn.request('POST', '/generate', body, {'Content-Type': 'application/json', 'X-Request-ID': rid,
                                                     'Connection': 'close'})
            resp = conn.getresponse()
            self.send_response(resp.status)
            for header in ('Content-Type', 'X-Request-ID', 'X-Pod', 'X-Model-Version'):
                if resp.getheader(header):
                    self.send_header(header, resp.getheader(header))
            self.send_header('X-Router', NAME)
            self.send_header('Connection', 'close')
            self.end_headers()
            while line := resp.readline():
                self.wfile.write(line)
                self.wfile.flush()
            finished = time.monotonic() - started
            conn.close()
        except OSError as error:
            if finished is None:
                try:
                    self.reply(502, {'error': f'backend: {error}', 'request_id': rid})
                except OSError:
                    pass
        finally:
            POOL.done(backend, finished if finished and finished > 0 else None)
            print(json.dumps({'event': 'route', 'request_id': rid, 'backend': backend.ip, 'key': key,
                              'cached': scores and backend.cached(key), 'scores': scores}), flush=True)
            self.close_connection = True


def main():
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8000))), Handler)
    server.daemon_threads = True
    print(json.dumps({'event': 'start', 'router': NAME, 'C': C, 'hit': HIT, 'miss': MISS, 'index': INDEX_SIZE}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
