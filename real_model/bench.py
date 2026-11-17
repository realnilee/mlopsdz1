"""Короткий опыт с настоящей моделью ДЗ 6+7 на GPU, вне Kubernetes, тем же HTTP-сервером.

TTFT — от отправки до первой непустой части с токенами. TPOT = (t_последней − t_первой) / (N − k1),
где N — число фактически сгенерированных токенов, k1 — токенов в первой части (обычно 1).
Ответ из одного токена сохраняется с TPOT = null.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import loadgen  # noqa: E402
from app.backends import file_sha256  # noqa: E402

# На Windows time.monotonic идёт шагами ~15,6 мс — сравнимо с TPOT. perf_counter тоже
# монотонен, но с разрешением QPC. В кластере (Linux) генератор не меняется: там шаг — наносекунды.
time.monotonic = time.perf_counter

HW = ROOT.parents[1]
ADAPTER = HW / 'дз 8-9-10' / 'hw8910-broken' / '.release-run' / 'artifact' / 'adapter'
GOLDEN = HW / 'дз 6-7' / 'mlops26-hw6-7' / 'hw67-broken' / 'data' / 'eval' / 'golden.jsonl'
OUT = ROOT / 'results' / 'real_model'
PORT, N, LIMIT = 8100, 8, 24


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'BACKEND': 'transformers', 'ADAPTER_DIR': str(ADAPTER), 'MODEL_VERSION': 'my-qwen-adapter-v2',
           'MODEL_SHA256': file_sha256(ADAPTER / 'adapter_model.safetensors'), 'MAX_CONCURRENCY': '1',
           'MAX_CHUNKS': str(LIMIT), 'HOST': '127.0.0.1', 'PORT': str(PORT), 'PYTHONUTF8': '1'}
    log = (OUT / 'server.log').open('w', encoding='utf-8')
    server = subprocess.Popen([sys.executable, '-m', 'app.server'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    started = time.monotonic()
    try:
        while True:
            try:
                ready = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/ready', timeout=2).read())
                if ready.get('ready'):
                    break
            except Exception:
                pass
            if server.poll() is not None or time.monotonic() - started > 300:
                raise SystemExit('сервер с моделью не поднялся, см. server.log')
            time.sleep(0.5)
        load_seconds = time.monotonic() - started
        info = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/info').read())
        prompts = [json.loads(x)['input'] for x in GOLDEN.read_text(encoding='utf-8').splitlines() if x.strip()][:N]
        rows = []
        for conc in (1, 2):
            t0 = time.monotonic()
            with ThreadPoolExecutor(conc) as pool:
                batch = list(pool.map(lambda i: loadgen.request_once(
                    f'http://127.0.0.1:{PORT}/generate', {'prompt': prompts[i], 'max_new_tokens': LIMIT},
                    f'gpu-c{conc}-{i}', 120), range(N)))
            wall = time.monotonic() - t0
            for i, row in enumerate(batch):
                times = row['chunk_times'] or []
                first_tokens = 1
                row.update(concurrency=conc, index=i, batch_seconds=wall, ttft_s=row['ttfc_s'],
                           tpot_s=((times[-1] - times[0]) / (row['tokens'] - first_tokens)
                                   if len(times) > 1 and row['tokens'] > first_tokens else None))
                rows.append(row)
        gpu = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.used,memory.total,driver_version',
                              '--format=csv,noheader'], capture_output=True, text=True).stdout.strip()
    finally:
        server.terminate()
        server.wait(timeout=30)
        log.close()
    loadgen_rows = OUT / 'requests.jsonl'
    loadgen_rows.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    summary = {'model_load_and_warmup_s': load_seconds, 'gpu': gpu, 'info': info, 'limit_tokens': LIMIT, 'requests': N,
               'per_concurrency': {}}
    for conc in (1, 2):
        ok = [r for r in rows if r['concurrency'] == conc and r['outcome'] == 'ok']
        tpots = [r['tpot_s'] for r in ok if r['tpot_s'] is not None]
        tokens = sum(r['tokens'] for r in ok)
        wall = ok[0]['batch_seconds'] if ok else None
        summary['per_concurrency'][conc] = {
            'ok': len(ok), 'errors': N - len(ok), 'batch_seconds': wall, 'output_tokens': tokens,
            'tokens_per_second': tokens / wall if wall else None,
            'ttft_mean_s': sum(r['ttft_s'] for r in ok) / len(ok), 'ttft_max_s': max(r['ttft_s'] for r in ok),
            'tpot_mean_s': sum(tpots) / len(tpots) if tpots else None, 'tpot_n': len(tpots),
            'queue_mean_s': sum(r['queue_s'] or 0 for r in ok) / len(ok)}
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
