"""HTTP-сервис ДЗ 11: потоковая выдача, очередь, ограничение параллельности, прогрев, метрики.

Каждый ответ и строка журнала несут request_id, имя Pod и фактическую версию модели.
История диалога приходит в запросе: Pod ничего не хранит между запросами.
"""
import asyncio
import hashlib
import json
import os
import re
import socket
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

from app.backends import SimulatedBackend, TransformersBackend


def _env(name, default, cast=float):
    return cast(os.environ.get(name, default))


CFG = {
    'backend': os.environ.get('BACKEND', 'simulated'),
    'model_dir': os.environ.get('MODEL_DIR', '/models'),
    'adapter_dir': os.environ.get('ADAPTER_DIR', ''),
    'model_version': os.environ.get('MODEL_VERSION', 'A'),
    'model_sha256': os.environ.get('MODEL_SHA256', ''),
    'warmup': _env('WARMUP_SECONDS', 20),
    'max_concurrency': _env('MAX_CONCURRENCY', 2, int),
    'max_queue': _env('MAX_QUEUE', 32, int),
    'chunk_delay': _env('CHUNK_DELAY', 0.05),
    'default_chunks': _env('DEFAULT_CHUNKS', 6, int),
    'max_chunks': _env('MAX_CHUNKS', 1024, int),
    'cache_size': _env('PREFIX_CACHE_SIZE', 16, int),
    'hit_cost': _env('CACHE_HIT_COST', 0.25),
    'miss_cost': _env('CACHE_MISS_COST', 0.6),
    'cold_penalty': _env('COLD_PENALTY', 2.0),
    'drain_seconds': _env('DRAIN_SECONDS', 5),
}
POD = os.environ.get('POD_NAME', socket.gethostname())
CONTROL = re.compile(r'CTRL-[A-Za-z0-9]+')

TTFC = Histogram('sim_ttfc_seconds', 'Время от приёма до первой части ответа',
                 buckets=(.05, .1, .25, .5, .75, 1, 1.5, 2, 3, 5, 8, 13, 20, 30))
DURATION = Histogram('sim_request_duration_seconds', 'Полная длительность ответа',
                     buckets=(.1, .25, .5, 1, 1.5, 2, 3, 5, 8, 13, 20, 30, 60))
QUEUE = Histogram('sim_queue_wait_seconds', 'Ожидание слота вычисления',
                  buckets=(0, .05, .1, .25, .5, 1, 2, 3, 5, 8, 13, 20, 30))
RUNNING = Gauge('sim_running', 'Запросы в вычислении')
WAITING = Gauge('sim_waiting', 'Запросы в очереди')
REQUESTS = Counter('sim_requests_total', 'Завершённые запросы по исходу', ['outcome'])
CACHE = Counter('sim_prefix_cache_total', 'Обращения к префиксному кешу', ['result'])
WARM = Gauge('sim_warm', '1 после завершения прогрева')
INFO = Gauge('sim_model_info', 'Загруженная версия', ['version', 'backend_mode'])


class State:
    def __init__(self):
        self.backend = TransformersBackend(CFG) if CFG['backend'] == 'transformers' else SimulatedBackend(CFG)
        self.warm = threading.Event()
        self.draining = False
        self.waiting = 0
        self.started = time.monotonic()
        self.heartbeat = time.monotonic()
        self.warm_error = None
        self.sem = None
        INFO.labels(self.backend.version, self.backend.mode).set(1)

    def warm_up(self):
        try:
            self.backend.warm_up()
            self.warm.set()
            WARM.set(1)
            log(event='warm', seconds=round(time.monotonic() - self.started, 3))
        except Exception as error:
            self.warm_error = f'{type(error).__name__}: {error}'
            log(event='warm_failed', error=self.warm_error)


STATE = State()


def log(**fields):
    print(json.dumps({'ts': time.time(), 'pod': POD, 'version': STATE.backend.version if 'STATE' in globals() else None,
                      **fields}, ensure_ascii=False), flush=True)


def identity():
    return {'pod': POD, 'version': STATE.backend.version, 'model_sha256': STATE.backend.sha256,
            'backend_mode': STATE.backend.mode}


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('тело должно быть JSON-объектом')
    prompt = payload.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        raise ValueError('prompt: непустая строка до 8000 символов')
    chunks = payload.get('max_new_tokens', CFG['default_chunks'])
    if type(chunks) is not int or not 1 <= chunks <= CFG['max_chunks']:
        raise ValueError(f"max_new_tokens: целое от 1 до {CFG['max_chunks']}")
    session = payload.get('session_id', '')
    prefix = payload.get('prefix', '')
    history = payload.get('history', [])
    if not isinstance(session, str) or len(session) > 128:
        raise ValueError('session_id: строка до 128 символов')
    if not isinstance(prefix, str) or len(prefix) > 20000:
        raise ValueError('prefix: строка до 20000 символов')
    if not isinstance(history, list) or len(history) > 50 or not all(isinstance(x, str) for x in history):
        raise ValueError('history: список строк, не более 50')
    return prompt, chunks, session, prefix, history


async def heartbeat():
    while True:
        STATE.heartbeat = time.monotonic()
        await asyncio.sleep(1)


async def lifespan(_):
    STATE.sem = asyncio.Semaphore(CFG['max_concurrency'])
    threading.Thread(target=STATE.warm_up, daemon=True).start()
    task = asyncio.create_task(heartbeat())
    log(event='start', config=CFG, **identity())
    yield
    task.cancel()
    log(event='stop')


app = FastAPI(lifespan=lifespan)
app.mount('/metrics', make_asgi_app())


@app.get('/health')
def health():
    """Процесс жив и принимает HTTP. Не говорит о прогреве: на нём держится исходный дефект."""
    return {'alive': True, **identity()}


@app.get('/live')
def live():
    stale = time.monotonic() - STATE.heartbeat
    return JSONResponse({'live': stale < 10, 'heartbeat_age': stale}, status_code=200 if stale < 10 else 500)


@app.get('/startup')
def startup():
    done = STATE.warm.is_set()
    return JSONResponse({'started': done, 'error': STATE.warm_error}, status_code=200 if done else 503)


@app.get('/ready')
def ready():
    ok = STATE.warm.is_set() and not STATE.draining
    return JSONResponse({'ready': ok, 'draining': STATE.draining, 'error': STATE.warm_error, **identity()},
                        status_code=200 if ok else 503)


@app.get('/info')
def info():
    return {**identity(), 'warm': STATE.warm.is_set(), 'artifact': STATE.backend.artifact,
            'uptime': time.monotonic() - STATE.started, 'config': CFG}


@app.get('/drain')
async def drain():
    """preStop: Pod выходит из Ready, пока Endpoints не обновятся; активные потоки дорабатывают."""
    STATE.draining = True
    log(event='drain', running=RUNNING._value.get(), waiting=STATE.waiting)
    await asyncio.sleep(CFG['drain_seconds'])
    return {'draining': True}


@app.post('/admin/reset-cache')
def reset_cache():
    STATE.backend.cache.clear()
    return {'reset': True, 'pod': POD}


@app.post('/generate')
async def generate(request: Request):
    rid = request.headers.get('x-request-id') or str(uuid.uuid4())
    client_ip = request.client.host if request.client else None
    headers = {'X-Request-ID': rid, 'X-Pod': POD, 'X-Model-Version': STATE.backend.version}
    body = await request.body()
    try:
        if len(body) > 65536:
            raise ValueError('тело больше 64 КБ')
        prompt, chunks, session, prefix, history = validate(json.loads(body))
    except ValueError as error:
        REQUESTS.labels('invalid').inc()
        return JSONResponse({'error': str(error), 'request_id': rid, **identity()}, 400, headers)
    if STATE.warm_error:
        REQUESTS.labels('error').inc()
        return JSONResponse({'error': STATE.warm_error, 'request_id': rid, **identity()}, 503, headers)
    if STATE.waiting >= CFG['max_queue']:
        REQUESTS.labels('rejected').inc()
        log(event='request', request_id=rid, session_id=session, client_ip=client_ip, outcome='rejected')
        return JSONResponse({'error': 'очередь заполнена', 'request_id': rid, **identity()}, 429, headers)
    warm = STATE.warm.is_set()
    accepted = time.monotonic()
    key = hashlib.sha256((prefix + '\x00' + '\x00'.join(history)).encode()).hexdigest()[:16]
    echo = sorted(set(CONTROL.findall(' '.join(history + [prompt]))))

    async def events():
        outcome, queued, first, n_chunks, n_tokens, cache = 'aborted', None, None, 0, 0, None
        STATE.waiting += 1
        WAITING.set(STATE.waiting)
        acquired = False
        try:
            async with STATE.sem:
                acquired = True
                STATE.waiting -= 1
                WAITING.set(STATE.waiting)
                RUNNING.inc()
                queued = time.monotonic() - accepted
                QUEUE.observe(queued)
                try:
                    yield _line(type='meta', request_id=rid, session_id=session, warm=warm, queue_seconds=queued,
                                client_ip=client_ip, **identity())
                    cache = await asyncio.to_thread(STATE.backend.prefill, key, warm)
                    CACHE.labels(cache).inc()
                    async for text, tokens in STATE.backend.decode(chunks, prompt):
                        n_chunks += 1
                        n_tokens += tokens
                        if first is None:
                            first = time.monotonic() - accepted
                            TTFC.observe(first)
                        yield _line(type='chunk', i=n_chunks, text=text, tokens=tokens)
                    outcome = 'ok'
                    yield _line(type='done', request_id=rid, chunks=n_chunks, tokens=n_tokens, cache=cache,
                                context_echo=echo, history_sha256=hashlib.sha256(json.dumps(history).encode()).hexdigest(),
                                **identity())
                finally:
                    RUNNING.dec()
        finally:
            if not acquired:
                STATE.waiting -= 1
                WAITING.set(STATE.waiting)
            total = time.monotonic() - accepted
            REQUESTS.labels(outcome).inc()
            if outcome == 'ok':
                DURATION.observe(total)
            log(event='request', request_id=rid, session_id=session, client_ip=client_ip, warm=warm, outcome=outcome,
                queue_s=queued, ttfc_s=first, total_s=total, chunks=n_chunks, cache=cache, echo=echo)

    return StreamingResponse(events(), media_type='application/x-ndjson', headers=headers)


def _line(**fields):
    return (json.dumps(fields, ensure_ascii=False) + '\n').encode()


def main():
    uvicorn.run(app, host=os.environ.get('HOST', '0.0.0.0'), port=int(os.environ.get('PORT', 8000)),
                timeout_graceful_shutdown=int(os.environ.get('GRACEFUL_SECONDS', 45)),
                log_level='warning', access_log=False)


if __name__ == '__main__':
    main()
