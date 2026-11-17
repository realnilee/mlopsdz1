"""Генератор нагрузки с открытой моделью: отправка по расписанию, а не после ответа.

Часы: time.monotonic для длительностей, time.time для сверки запланированного и
фактического момента между Pod одного узла. Каждое соединение новое (Connection: close),
поэтому распределение Service не искажается keep-alive.
"""
import argparse
import hashlib
import http.client
import json
import os
import random
import sys
import threading
import time
import urllib.parse
import uuid

OUTCOMES = ('ok', 'rejected', 'http_4xx', 'http_5xx', 'aborted', 'timeout', 'connect_error')


def prefix_text(session_id, length):
    seed = hashlib.sha256(session_id.encode()).hexdigest()
    return (seed * (length // 64 + 1))[:length]


def make_schedule(rate, duration, clients, hot_share=None, sessions_per_client=10, prefix_len=2000,
                  chunks=6, seed=42, warm_seconds=0.0):
    """Пуассоновский поток. hot_share — доля запросов через клиента 0; остальные — поровну."""
    rng = random.Random(seed)
    items, t, seq = [], 0.0, 0
    total = warm_seconds + duration
    while True:
        t += rng.expovariate(rate)
        if t >= total:
            break
        if hot_share is None:
            client = rng.randrange(clients)
        else:
            client = 0 if rng.random() < hot_share else 1 + rng.randrange(clients - 1)
        session = f'c{client}-s{rng.randrange(sessions_per_client)}'
        items.append({'seq': seq, 'offset': round(t, 4), 'client': client, 'session_id': session,
                      'prefix_len': prefix_len, 'max_new_tokens': chunks, 'phase': 'warm' if t < warm_seconds else 'measure',
                      'prompt': f'Вопрос {seq} в сессии {session}'})
        seq += 1
    return items


def request_once(url, payload, request_id, timeout):
    parts = urllib.parse.urlsplit(url)
    row = {'request_id': request_id, 'sent_wall': time.time(), 'outcome': None, 'status': None, 'pod': None,
           'version': None, 'warm': None, 'chunks': 0, 'tokens': 0, 'ttfc_s': None, 'total_s': None,
           'queue_s': None, 'cache': None, 'error': None, 'echo': None, 'first_wall': None, 'done_wall': None,
           'chunk_times': None}
    start = time.monotonic()
    stamps = []
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    started_stream = False
    try:
        body = json.dumps(payload).encode()
        conn.request('POST', parts.path or '/generate', body, {'Content-Type': 'application/json',
                                                               'X-Request-ID': request_id, 'Connection': 'close'})
        resp = conn.getresponse()
        row['status'] = resp.status
        row['pod'] = resp.getheader('X-Pod')
        row['version'] = resp.getheader('X-Model-Version')
        if resp.status != 200:
            data = resp.read()
            row['outcome'] = ('rejected' if resp.status == 429 else 'http_5xx' if resp.status >= 500 else 'http_4xx')
            row['error'] = data[:200].decode('utf-8', 'replace')
            return row
        started_stream = True
        done = False
        while True:
            line = resp.readline()
            if not line:
                break
            event = json.loads(line)
            if event['type'] == 'meta':
                row.update(pod=event['pod'], version=event['version'], warm=event['warm'], queue_s=event['queue_seconds'])
            elif event['type'] == 'chunk':
                now = time.monotonic()
                if row['ttfc_s'] is None:
                    row['ttfc_s'] = now - start
                    row['first_wall'] = time.time()
                stamps.append(round(now - start, 4))
                row['chunks'] += 1
                row['tokens'] += event.get('tokens', 1)
            elif event['type'] == 'done':
                done = True
                row.update(cache=event['cache'], echo=event['context_echo'])
        row['outcome'] = 'ok' if done else 'aborted'
        if not done:
            row['error'] = 'поток закончился без события done'
    except TimeoutError as error:
        row['outcome'], row['error'] = 'timeout', str(error)
    except (ConnectionError, http.client.HTTPException, OSError, json.JSONDecodeError) as error:
        row['outcome'] = 'aborted' if started_stream else 'connect_error'
        row['error'] = f'{type(error).__name__}: {error}'
    finally:
        conn.close()
        row['total_s'] = time.monotonic() - start
        row['done_wall'] = time.time()
        row['chunk_times'] = stamps if len(stamps) <= 64 else None
    return row


RETRYABLE = {'connect_error', 'aborted', 'http_5xx'}


def execute(url, item, timeout, retries, out, lock, tags):
    payload = {'prompt': item['prompt'], 'max_new_tokens': item['max_new_tokens'], 'session_id': item['session_id'],
               'prefix': prefix_text(item['session_id'], item['prefix_len']), 'history': item.get('history', [])}
    logical = item.get('logical_id', f"{tags.get('run', 'r')}-{item['seq']}")
    for attempt in range(retries + 1):
        row = request_once(url, payload, f'{logical}-a{attempt}-{uuid.uuid4().hex[:6]}', timeout)
        row.update(tags, seq=item['seq'], logical_id=logical, attempt=attempt, client=item['client'],
                   session_id=item['session_id'], phase=item['phase'], planned_wall=item.get('planned_wall'),
                   prefix_len=item['prefix_len'], max_new_tokens=item['max_new_tokens'],
                   client_pod=os.environ.get('POD_NAME'), client_ip=os.environ.get('POD_IP'))
        if row['planned_wall'] is not None and attempt == 0:
            row['send_lag_s'] = row['sent_wall'] - row['planned_wall']
        with lock:
            out.write(json.dumps(row, ensure_ascii=False) + '\n')
            out.flush()
        if row['outcome'] not in RETRYABLE:
            break
        time.sleep(0.5)


def run_schedule(url, items, start_at, timeout, retries, out_path, tags):
    lock = threading.Lock()
    threads = []
    with open(out_path, 'a', encoding='utf-8') as out:
        for item in items:
            item['planned_wall'] = start_at + item['offset']
            delay = item['planned_wall'] - time.time()
            if delay > 0:
                time.sleep(delay)
            thread = threading.Thread(target=execute, args=(url, item, timeout, retries, out, lock, tags), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout + 30)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://sim-api:8000/generate')
    p.add_argument('--schedule', help='JSON со списком запросов')
    p.add_argument('--client', type=int, help='выполнить только записи этого клиента')
    p.add_argument('--rate', type=float)
    p.add_argument('--duration', type=float)
    p.add_argument('--chunks', type=int, default=6)
    p.add_argument('--sessions', type=int, default=10)
    p.add_argument('--start-at', type=float, default=0)
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--retries', type=int, default=0)
    p.add_argument('--out', required=True)
    p.add_argument('--tag', action='append', default=[], help='key=value в каждую строку')
    args = p.parse_args()
    tags = dict(t.split('=', 1) for t in args.tag)
    if args.schedule:
        items = json.load(open(args.schedule, encoding='utf-8'))
    else:
        items = make_schedule(args.rate, args.duration, 1, sessions_per_client=args.sessions, chunks=args.chunks,
                              seed=int(time.time()))
        for item in items:
            item['client'] = args.client or 0
    if args.client is not None:
        items = [i for i in items if i['client'] == args.client]
    start_at = args.start_at or time.time() + 1
    run_schedule(args.url, items, start_at, args.timeout, args.retries, args.out, tags)
    print(f'выполнено {len(items)} запланированных запросов', file=sys.stderr)


if __name__ == '__main__':
    main()
