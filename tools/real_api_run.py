"""Реальный smoke и замер 8+8. Пишет все попытки, включая ошибки, в evidence/load."""
import csv
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flows.hw67 import HW67
from tests_integration.harness import ENV, free_port, http

WORKSPACE = ROOT / '.release-run'
OUT = ROOT / 'evidence'
N_REQUESTS = 8


def timed_get(url):
    try:
        return http('GET', url)
    except OSError as error:
        return None, {'error': type(error).__name__}


def smoke(base, process, started):
    log = {'startup': []}
    while True:
        status, body = timed_get(base + '/ready')
        health, _ = timed_get(base + '/health')
        moment = round(time.monotonic() - started, 2)
        if not log['startup'] or log['startup'][-1]['ready_status'] != status:
            generate = http('POST', base + '/generate', {'prompt': 'x', 'max_new_tokens': 1})[0] if health == 200 else None
            log['startup'].append({'t': moment, 'health_status': health, 'ready_status': status, 'generate_status': generate})
        if status == 200:
            log['ready_body'] = body
            break
        if process.poll() is not None:
            raise SystemExit(f'сервис завершился: {process.returncode}')
        time.sleep(.25)
    question = 'Question:\nWhat is the chemical symbol for sodium?\n\nChoices:\nA. S\nB. Na\nC. So\nD. N'
    log['valid'] = dict(zip(('status', 'body'), http('POST', base + '/generate', {'prompt': question})))
    log['invalid'] = [{'payload': p, 'status': http('POST', base + '/generate', p)[0]}
                      for p in ({'prompt': 'x', 'max_new_tokens': 100000}, {'prompt': ''}, {'prompt': 'x', 'max_new_tokens': '8'})]
    return log


def load(base, rows, expected):
    attempts, summary = [], []
    for mode in (1, 2):
        lock = threading.Lock()

        def call(item):
            index, row = item
            start = time.perf_counter()
            status, error, body = None, None, {}
            try:
                status, body = http('POST', base + '/generate', {'prompt': row['input']})
                if status != 200:
                    error = body.get('error')
            except OSError as exc:
                error = f'{type(exc).__name__}: {exc}'
            record = {'mode': mode, 'index': index, 'id': row['id'], 'status': status, 'error': error,
                      'client_seconds': time.perf_counter() - start, 'queue_seconds': body.get('queue_seconds'),
                      'generation_seconds': body.get('generation_seconds'), 'output_tokens': body.get('output_tokens'),
                      'input_tokens': body.get('input_tokens'), 'model_id': body.get('model_id'),
                      'request_id': body.get('request_id'), 'answer': body.get('answer'),
                      'same_as_eval': body.get('answer') == expected.get(row['id'])}
            with lock:
                attempts.append(record)

        started = time.perf_counter()
        with ThreadPoolExecutor(mode) as pool:
            list(pool.map(call, enumerate(rows)))
        batch = time.perf_counter() - started
        done = [a for a in attempts if a['mode'] == mode and a['status'] == 200]
        latency = [a['client_seconds'] for a in done]
        summary.append({'concurrency': mode, 'n': len(rows), 'ok': len(done), 'errors': len(rows) - len(done),
                        'batch_seconds': round(batch, 3), 'ok_per_second': round(len(done) / batch, 3),
                        'mean_latency': round(sum(latency) / len(latency), 3) if latency else None,
                        'max_latency': round(max(latency), 3) if latency else None,
                        'mean_queue': round(sum(a['queue_seconds'] for a in done) / len(done), 3) if done else None,
                        'mean_generation': round(sum(a['generation_seconds'] for a in done) / len(done), 3) if done else None,
                        'total_output_tokens': sum(a['output_tokens'] for a in done),
                        'same_as_eval': sum(a['same_as_eval'] for a in done)})
    return attempts, summary


def main():
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, '-m', 'app.api', '--workspace', str(WORKSPACE), '--port', str(port),
                                '--warmup-seconds', '0', '--backend', 'real'], cwd=ROOT, env=ENV)
    try:
        while timed_get(base + '/health')[0] != 200:
            if process.poll() is not None or time.monotonic() - started > 300:
                raise SystemExit('сервис не поднялся')
            time.sleep(.1)
        log = smoke(base, process, started)
        log['debug_route_status'] = timed_get(base + '/debug')[0]
        rows = [json.loads(x) for x in (HW67 / 'data/eval/golden.jsonl').read_text(encoding='utf-8').split('\n') if x.strip()][:N_REQUESTS]
        expected = {o['id']: o['raw'] for o in json.loads((HW67 / 'metrics/candidate-outputs.json').read_text(encoding='utf-8'))}
        attempts, summary = load(base, rows, expected)
    finally:
        process.terminate()
        process.wait(timeout=30)
    for name in ('api', 'load'):
        (OUT / name).mkdir(parents=True, exist_ok=True)
    (OUT / 'api/smoke.json').write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')
    with (OUT / 'load/requests.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(attempts[0]))
        writer.writeheader()
        writer.writerows(attempts)
    (OUT / 'load/summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(log, ensure_ascii=False, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
