"""Опыт C: HPA 2→3→2 по CPU, горячая реплика при сохранённой привязке, две тревоги.

Фаза 1 (0–150 с): 80% через одного клиента, Service с ClientIP, все запросы — промахи
кеша (вычислительная нагрузка). Средний CPU растёт, HPA добавляет третью реплику,
но новые клиентские IP не появляются — она почти не получает запросов.
Фаза 2 (150–240 с): то же расписание через обычный Service.
Фаза 3: нагрузка снята, ожидание возврата к двум репликам после окна стабилизации.
"""
import json
import time

from kube import (RESULTS, Watcher, clients, collect, deploy, fresh_clients, kjson, kubectl, loadgen, say, sim_pods,
                  start_load, wait_ready, write_jsonl)

OUT = RESULTS / 'exp_c'
RATE, PHASE1, PHASE2 = 3.0, 150, 90


def schedule(offset, duration, seed):
    items = loadgen.make_schedule(RATE, duration, 4, hot_share=0.8, sessions_per_client=5000, prefix_len=2000,
                                  chunks=6, seed=seed)
    for item in items:
        item['offset'] = round(item['offset'] + offset, 4)
        item['phase'] = 'affinity' if offset == 0 else 'plain'
    return items


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    deploy('A', fixed=True, hpa=True)
    wait_ready(2)
    (OUT / 'hpa.json').write_text(json.dumps(kjson('get', 'hpa', 'sim'), ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'metrics-api.txt').write_text(kubectl('get', 'apiservices', 'v1beta1.metrics.k8s.io') + '\n'
                                         + kubectl('top', 'pods', '-l', 'app=sim-api'), encoding='utf-8')
    found = fresh_clients()
    first, second = schedule(0, PHASE1, 31), schedule(PHASE1, PHASE2, 32)
    start_at = time.time() + 8
    events = {'start_at': start_at, 'load_end': start_at + PHASE1 + PHASE2}
    with Watcher(OUT, hpa=True, alerts=True, interval=2.0):
        procs = []
        for i, (pod, _) in enumerate(found):
            procs.append(start_load(pod, first, 'http://sim-affinity:8000/generate', '/tmp/c1.jsonl', start_at,
                                    client=i, tags={'run': 'hpa-affinity'}))
            procs.append(start_load(pod, second, 'http://sim:8000/generate', '/tmp/c2.jsonl', start_at, client=i,
                                    tags={'run': 'hpa-plain'}))
        for proc in procs:
            proc.wait(timeout=PHASE1 + PHASE2 + 300)
        say('нагрузка снята, жду возврата к 2 репликам')
        deadline = time.time() + 600
        while time.time() < deadline:
            hpa = kjson('get', 'hpa', 'sim')['status']
            if hpa.get('currentReplicas') == 2 and len(sim_pods()) == 2:
                break
            time.sleep(5)
        events['back_to_min'] = time.time()
        time.sleep(60)  # тревоги должны вернуться в норму
    rows = []
    for pod, _ in found:
        rows += collect(pod, '/tmp/c1.jsonl') + collect(pod, '/tmp/c2.jsonl')
    write_jsonl(OUT / 'requests.jsonl', rows)
    events['clients'] = found
    (OUT / 'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'hpa-describe.txt').write_text(kubectl('describe', 'hpa', 'sim'), encoding='utf-8')
    say('строк', len(rows))


if __name__ == '__main__':
    main()
