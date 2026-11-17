"""Опыт B: обычный Service против sessionAffinity: ClientIP, равномерный трафик и 80% через одного клиента.

Одинаковое расписание для обеих политик; перед каждым прогоном — новые клиентские Pod
(новые IP, чистая привязка) и сброс префиксного кеша. Фаза прогрева 10 с, замер 120 с.
"""
import itertools
import os
import json
import sys
import time

from kube import (RESULTS, clients, collect, deploy, fresh_clients, kjson, kubectl, loadgen, reset_caches, say,
                  start_load, wait_ready, write_jsonl)

OUT = RESULTS / 'exp_b'
RATE, MEASURE, WARM = 3.5, 120, 10
POLICIES = {'affinity': 'http://sim-affinity:8000/generate', 'plain': 'http://sim:8000/generate',
            'cacheaware': 'http://sim-router:8000/generate'}
# HW11_POLICIES=cacheaware — догнать только выбранные политики, не перезапуская остальные.
SELECTED = [p for p in os.environ.get('HW11_POLICIES', ','.join(POLICIES)).split(',') if p]
TRAFFIC = {'uniform': None, 'hot80': 0.8}


def schedule(traffic):
    return loadgen.make_schedule(RATE, MEASURE, 4, hot_share=TRAFFIC[traffic], sessions_per_client=10,
                                 prefix_len=2000, chunks=6, seed=2026, warm_seconds=WARM)


def record_network():
    proxy = kubectl('-n', 'kube-system', 'get', 'configmap', 'kube-proxy', '-o', 'jsonpath={.data.config\\.conf}')
    mode = [line.strip() for line in proxy.splitlines() if line.strip().startswith('mode:')]
    services = {name: kjson('get', 'svc', name)['spec'] for name in ('sim', 'sim-affinity')}
    info = {'kube_proxy_mode': mode, 'kube_proxy_note': 'пусто = iptables по умолчанию в kind',
            'services': {k: {f: v.get(f) for f in ('clusterIP', 'sessionAffinity', 'sessionAffinityConfig')}
                         for k, v in services.items()},
            'client_connection': 'новое TCP-соединение на каждый запрос (Connection: close), keep-alive не используется',
            'params': {'rate_rps': RATE, 'measure_s': MEASURE, 'warm_s': WARM, 'clients': 4, 'sessions_per_client': 10,
                       'prefix_len_chars': 2000, 'chunks': 6}}
    (OUT / 'network.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')


def run(rep, traffic, policy):
    name = f'{traffic}-{policy}-r{rep}'
    items = schedule(traffic)
    found = fresh_clients()
    wait_ready(2)
    reset_caches(found[0][0])
    sims = {p['metadata']['name']: p['status']['podIP'] for p in kjson('get', 'pods', '-l', 'app=sim-api')['items']}
    start_at = time.time() + 8
    procs = [start_load(pod, items, POLICIES[policy], f'/tmp/{name}.jsonl', start_at, client=i,
                        tags={'run': name, 'traffic': traffic, 'policy': policy, 'rep': rep})
             for i, (pod, _) in enumerate(found)]
    for proc in procs:
        proc.wait(timeout=WARM + MEASURE + 200)
    rows = []
    for pod, _ in found:
        rows += collect(pod, f'/tmp/{name}.jsonl')
    write_jsonl(OUT / f'{name}.jsonl', rows)
    meta = {'run': name, 'start_at': start_at, 'measure_from': start_at + WARM, 'measure_to': start_at + WARM + MEASURE,
            'clients': [{'pod': p, 'ip': ip} for p, ip in found], 'servers': sims,
            'cache_state': 'сброшен перед прогоном, затем 10 с прогрева без учёта',
            'scheduled': len(items), 'rows': len(rows)}
    (OUT / f'{name}.meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    by_pod = {}
    for r in rows:
        if r['phase'] == 'measure':
            by_pod[r.get('pod')] = by_pod.get(r.get('pod'), 0) + 1
    say(name, 'по Pod:', by_pod, 'исходы:', {o: sum(r['outcome'] == o for r in rows) for o in {r['outcome'] for r in rows}})


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    deploy('A', fixed=True)
    wait_ready(2)
    record_network()
    for traffic in TRAFFIC:
        (OUT / f'schedule-{traffic}.json').write_text(json.dumps(schedule(traffic)), encoding='utf-8')
    reps = [int(x) for x in sys.argv[1:]] or [1, 2, 3]
    for rep, traffic, policy in itertools.product(reps, TRAFFIC, SELECTED):
        run(rep, traffic, policy)
