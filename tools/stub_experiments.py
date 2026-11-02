"""STUB-опыты для отчёта: отрицательные выпуски с SHA-256 указателя и A → B → A на FastAPI."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests_integration.harness import ENV, free_port, http, sha

FLOW = [sys.executable, '-m', 'flows.release_flow']


def flow(ws, *args):
    done = subprocess.run([*FLOW, '--workspace', str(ws), *args], cwd=ROOT, env=ENV, capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=180)
    marks = [x for x in done.stdout.splitlines() if x.startswith(('PREPARED', 'CACHE_HIT', 'PUBLISHED', 'ALREADY', 'FAILED'))]
    print(f'$ release_flow {" ".join(args)} -> exit {done.returncode}: {marks}', flush=True)
    return done.returncode


def serve_and_ask(ws):
    port = free_port()
    process = subprocess.Popen([sys.executable, '-m', 'app.api', '--workspace', str(ws), '--port', str(port), '--warmup-seconds', '0'],
                               cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, f'http://127.0.0.1:{port}'


def wait_ready(base):
    for _ in range(300):
        try:
            if http('GET', base + '/ready')[0] == 200:
                return
        except OSError:
            pass
        time.sleep(.1)
    raise SystemExit('нет готовности')


def ask(base):
    _, body = http('POST', base + '/generate', {'prompt': 'q', 'max_new_tokens': 4})
    return {k: body[k] for k in ('answer', 'model_id')}


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = Path(tmp) / 'negative'
        flow(ws, 'init')
        report = json.loads((ws / 'report-B.json').read_text(encoding='utf-8'))
        (ws / 'report-nan.json').write_text(json.dumps({**report, 'metrics': {'accuracy': float('nan'), 'format_rate': 1.0}}), encoding='utf-8')
        for name in ('report-incomplete.json', 'report-reject.json', 'report-A.json', 'report-nan.json'):
            before = sha(ws / 'accepted.json')
            code = flow(ws, 'run', '--report', name)
            print(f'  {name}: exit={code} accepted before={before[:16]} after={sha(ws / "accepted.json")[:16]} unchanged={before == sha(ws / "accepted.json")}')

        ws = Path(tmp) / 'aba'
        flow(ws, 'init')
        process, base = serve_and_ask(ws)
        wait_ready(base)
        print('сервис запущен на A:', ask(base))
        flow(ws, 'run', '--candidate', 'B')
        print('B опубликован, перезапуска не было:', ask(base))
        process.terminate()
        process.wait(timeout=20)
        process, base = serve_and_ask(ws)
        wait_ready(base)
        print('после перезапуска:', ask(base))
        flow(ws, 'run', '--candidate', 'A')
        print('откат опубликован, перезапуска не было:', ask(base))
        process.terminate()
        process.wait(timeout=20)
        process, base = serve_and_ask(ws)
        wait_ready(base)
        print('после перезапуска:', ask(base))
        process.terminate()
        process.wait(timeout=20)
        print('журнал:', [(r['model_id'], r['release_id'][:8]) for r in map(json.loads, (ws / 'publications.jsonl').read_text(encoding='utf-8').splitlines())])


if __name__ == '__main__':
    main()
