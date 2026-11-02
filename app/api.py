"""FastAPI-сервис. Тот же HTTP-контракт и те же аргументы запуска, что у service.py."""
import argparse
import asyncio
import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import release
from api_contract import StubBackend, generation_limit, validate_generate

MAX_BODY_BYTES = 65536
MAX_WAITING = 16


def real_backend(manifest, root):
    from app.backends import TransformersBackend
    return TransformersBackend(manifest, root)


def create_app(root, backend_factory=StubBackend, warmup_seconds=0.0, debug_routes=False, max_concurrency=1):
    root = Path(root)
    # Снимок на момент старта: смена accepted.json не меняет модель в памяти.
    manifest = json.loads((root / 'accepted.json').read_text(encoding='utf-8'))
    release.verify_artifact(manifest, root)
    limit = generation_limit(manifest)
    backend = backend_factory(manifest, root)
    ready = threading.Event()
    state = {'error': None, 'waiting': 0}

    def warm_up():
        try:
            backend.warm_up()
            time.sleep(warmup_seconds)
            ready.set()
        except Exception as error:
            state['error'] = f'{type(error).__name__}: {error}'

    @asynccontextmanager
    async def lifespan(_):
        state['compute'] = asyncio.Semaphore(max_concurrency)
        threading.Thread(target=warm_up, daemon=True).start()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    def health():
        return {'alive': True, 'backend': type(backend).__name__}

    @app.get('/ready')
    def is_ready():
        if ready.is_set():
            return {'ready': True, 'model_id': backend.loaded_id, 'runtime_sha256': backend.runtime_sha256}
        return JSONResponse({'ready': False, 'error': state['error']}, status_code=503)

    if debug_routes:
        @app.get('/debug')
        def debug():
            return {'loaded_id': backend.loaded_id, 'calls': backend.calls, 'warmup_finished': ready.is_set()}

    @app.post('/generate')
    async def generate(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({'error': 'body too large'}, status_code=413)
        try:
            prompt, tokens = validate_generate(json.loads(body), limit)
        except ValueError as error:
            return JSONResponse({'error': str(error)}, status_code=400)
        if not ready.is_set():
            return JSONResponse({'error': 'модель ещё не готова'}, status_code=503)
        if state['waiting'] >= MAX_WAITING:
            return JSONResponse({'error': 'очередь заполнена'}, status_code=429)
        started = time.perf_counter()
        state['waiting'] += 1
        try:
            async with state['compute']:
                acquired = time.perf_counter()
                result = await run_in_threadpool(backend.generate, prompt, tokens)
        finally:
            state['waiting'] -= 1
        result.update(request_id=str(uuid.uuid4()), model_id=backend.loaded_id,
                      runtime_sha256=backend.runtime_sha256, queue_seconds=acquired - started,
                      generation_seconds=time.perf_counter() - acquired)
        return result

    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workspace', type=Path, default=Path('.lab'))
    p.add_argument('--port', type=int, default=8091)
    p.add_argument('--warmup-seconds', type=float, default=5)
    p.add_argument('--backend', choices=['stub', 'real'], default='stub')
    args = p.parse_args()
    stub = args.backend == 'stub'
    app = create_app(args.workspace, StubBackend if stub else real_backend, args.warmup_seconds, debug_routes=stub)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
