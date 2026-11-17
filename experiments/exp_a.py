"""Опыт A: прогрев (дефект и исправление), rolling update A→B под потоками,
неготовая B и откат, принудительное завершение Pod."""
import json
import sys
import time

from kube import (RESULTS, Watcher, clients, collect, deploy, helm, kjson, kubectl, loadgen, outcome_counts,
                  ready_sim_pods, say, start_load, wait_ready, write_jsonl)

OUT = RESULTS / 'exp_a'
URL = 'http://sim:8000/generate'


def schedule(rate, duration, seed, long_streams=()):
    items = loadgen.make_schedule(rate, duration, 1, sessions_per_client=20, seed=seed)
    for offset, chunks in long_streams:
        items.append({'seq': 10000 + len(items), 'offset': offset, 'client': 0, 'session_id': 'long', 'prefix_len': 200,
                      'max_new_tokens': chunks, 'phase': 'long_stream', 'prompt': 'Длинный потоковый ответ'})
    return sorted(items, key=lambda i: i['offset'])


def run_phase(name, items, action, retries=0, settle=15):
    """Нагрузка из одного клиента + действие над стендом в заданный момент."""
    client = clients()[0][0]
    directory = OUT / name
    duration = max(i['offset'] for i in items)
    start_at = time.time() + 5
    with Watcher(directory):
        proc = start_load(client, items, URL, f'/tmp/{name}.jsonl', start_at, retries=retries, tags={'run': name})
        action(start_at)
        while time.time() < start_at + duration + settle:
            time.sleep(1)
        proc.wait(timeout=180)
    rows = collect(client, f'/tmp/{name}.jsonl')
    write_jsonl(directory / 'requests.jsonl', rows)
    meta = {'start_at': start_at, 'rows': len(rows), 'outcomes': outcome_counts(rows),
            'cold_served': sum(1 for r in rows if r.get('warm') is False),
            'versions': sorted({r['version'] for r in rows if r.get('version')})}
    (directory / 'summary.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    say(name, json.dumps(meta, ensure_ascii=False))
    return rows


def at(start_at, offset, fn):
    while time.time() < start_at + offset:
        time.sleep(0.2)
    fn()


def delete_one_pod(start_at, offset=8, force=False):
    def act():
        victim = ready_sim_pods()[0]['metadata']['name']
        say('удаляю Pod', victim, 'force' if force else '')
        args = ['delete', 'pod', victim, '--wait=false']
        if force:
            args += ['--grace-period=0', '--force']
        kubectl(*args, check=False)
    at(start_at, offset, act)


def warmup(fixed):
    deploy('A', fixed=fixed)
    wait_ready(2)
    items = schedule(4, 60, seed=11)
    run_phase('warmup-fixed' if fixed else 'warmup-defect', items, lambda s: delete_one_pod(s))
    wait_ready(2)


def rollout():
    deploy('A', fixed=True)
    wait_ready(2)
    items = schedule(3, 120, seed=12, long_streams=[(5, 1000), (20, 600)])
    run_phase('rollout-A-to-B', items, lambda s: at(s, 12, lambda: deploy('B', fixed=True, wait=False)))
    wait_ready(2)


def broken_and_rollback():
    items = schedule(2, 150, seed=13)

    def act(start_at):
        at(start_at, 5, lambda: deploy('B', fixed=True, wait=False, extra=('--set', 'model.sha256=' + '0' * 64)))
        at(start_at, 75, lambda: helm('rollback', 'sim', str(rollback_target), '--timeout', '180s'))

    rollback_target = target_revision_a()
    say('откат будет к ревизии', rollback_target)
    run_phase('broken-B-and-rollback', items, act)
    wait_ready(2)
    (OUT / 'helm-history.json').write_text(helm('history', 'sim', '-o', 'json'), encoding='utf-8')


def target_revision_a():
    """Последняя ревизия с исправными probes и версией A."""
    best = None
    for h in json.loads(helm('history', 'sim', '-o', 'json')):
        values = json.loads(helm('get', 'values', 'sim', '--revision', str(h['revision']), '-o', 'json')) or {}
        if values.get('model', {}).get('version', 'A') == 'A' and values.get('probes', {}).get('fixed', True) is not False:
            best = h['revision']
    return best


def forced_kill():
    deploy('A', fixed=True)
    wait_ready(2)
    items = schedule(3, 75, seed=14, long_streams=[(5, 400), (6, 400)])
    run_phase('forced-kill', items, lambda s: delete_one_pod(s, offset=15, force=True), retries=2)
    wait_ready(2)


STEPS = {'warmup-defect': lambda: warmup(False), 'warmup-fixed': lambda: warmup(True),
         'rollout': rollout, 'broken': broken_and_rollback, 'kill': forced_kill}

if __name__ == '__main__':
    for step in sys.argv[1:] or list(STEPS):
        say('=== шаг', step)
        STEPS[step]()
