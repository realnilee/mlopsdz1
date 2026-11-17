"""Сводки и графики из сырых журналов results/. Перцентили — линейная интерполяция по успешным ответам."""
import csv
import json
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiments'))
RES = ROOT / 'results'
FIG = RES / 'figures'
KUBECTL = r'C:\Program Files\Docker\Docker\resources\bin\kubectl.exe'
FAIL = ('rejected', 'http_4xx', 'http_5xx', 'aborted', 'timeout', 'connect_error')


def load(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]


def pct(values, q):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def stats(rows, scheduled=None):
    first = [r for r in rows if r.get('attempt', 0) == 0]
    ok = [r for r in rows if r['outcome'] == 'ok']
    ttfc = [r['ttfc_s'] for r in ok if r['ttfc_s'] is not None]
    total = [r['total_s'] for r in ok]
    counts = Counter(r['outcome'] for r in rows)
    out = {'scheduled': scheduled if scheduled is not None else len(first), 'sent': len(first), 'attempts': len(rows),
           'completed': counts['ok'], 'rejected': counts['rejected'], 'aborted': counts['aborted'],
           'timeout': counts['timeout'], 'http_5xx': counts['http_5xx'], 'connect_error': counts['connect_error'],
           'error_share': round(sum(counts[o] for o in FAIL) / len(rows), 4) if rows else None,
           'n_ok': len(ok), 'ttfc_p50': pct(ttfc, .5), 'ttfc_p95': pct(ttfc, .95),
           'total_p50': pct(total, .5), 'total_p95': pct(total, .95)}
    lags = [r['send_lag_s'] for r in first if r.get('send_lag_s') is not None]
    if lags:
        out.update(send_lag_p50=pct(lags, .5), send_lag_max=max(lags))
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def timeline(path):
    return [r for r in load(path) if 'pods' in r]


def ready_endpoints(snap, version=None):
    names = {p['name']: p for p in snap['pods']}
    return sum(1 for e in snap['endpoints'] if e['ready'] and (version is None or names.get(e['pod'], {}).get('version') == version))


def exp_a():
    out = {}
    for name in ('warmup-defect', 'warmup-fixed', 'rollout-A-to-B', 'broken-B-and-rollback', 'forced-kill'):
        d = RES / 'exp_a' / name
        rows, tl, meta = load(d / 'requests.jsonl'), timeline(d / 'timeline.jsonl'), json.loads((d / 'summary.json').read_text(encoding='utf-8'))
        t0 = meta['start_at']
        info = {'stats': stats(rows), 'cold_served': sum(1 for r in rows if r.get('warm') is False),
                'versions': Counter(r.get('version') for r in rows if r['outcome'] == 'ok')}
        seen, first_seen, first_ready = {}, {}, {}
        for snap in tl:
            for p in snap['pods']:
                first_seen.setdefault(p['name'], snap['ts'] - t0)
                if p['ready']:
                    first_ready.setdefault(p['name'], snap['ts'] - t0)
                seen[p['name']] = p
        info['pods'] = {n: {'version': p['version'], 'first_seen_s': round(first_seen[n], 1),
                            'ready_s': round(first_ready[n], 1) if n in first_ready else None, 'restarts': p['restarts']}
                        for n, p in seen.items()}
        cold = [r for r in rows if r.get('warm') is False]
        if cold:
            info['cold_window_s'] = [round(min(r['sent_wall'] for r in cold) - t0, 1), round(max(r['sent_wall'] for r in cold) - t0, 1)]
            info['cold_ttfc_p50'] = pct([r['ttfc_s'] for r in cold if r['ttfc_s']], .5)
        longs = [r for r in rows if r['phase'] == 'long_stream']
        info['long_streams'] = [{'sent_s': round(r['sent_wall'] - t0, 1), 'done_s': round(r['done_wall'] - t0, 1),
                                 'pod': r['pod'], 'version': r['version'], 'outcome': r['outcome'], 'chunks': r['chunks']}
                                for r in longs]
        if name == 'broken-B-and-rollback':
            ok = sorted((r for r in rows if r['outcome'] == 'ok'), key=lambda r: r['done_wall'])
            info['versions_first_60s'] = Counter(r['version'] for r in ok if r['sent_wall'] - t0 < 60)
            info['versions_last_20'] = Counter(r['version'] for r in ok[-20:])
            broken = [n for n, p in info['pods'].items() if p['version'] == 'B' and p['ready_s'] is None]
            info['broken_pods'] = broken
            info['broken_in_ready_endpoints'] = sum(1 for s in tl for e in s['endpoints'] if e['pod'] in broken and e['ready'])
        if name == 'forced-kill':
            info['failed_attempts'] = [{'logical_id': r['logical_id'], 'attempt': r['attempt'], 'outcome': r['outcome'],
                                        'pod': r['pod'], 'sent_s': round(r['sent_wall'] - t0, 1), 'error': (r['error'] or '')[:80]}
                                       for r in rows if r['outcome'] != 'ok']
            retried = {r['logical_id'] for r in rows if r['attempt'] > 0}
            info['retried_logical'] = len(retried)
            info['retried_then_ok'] = sum(1 for r in rows if r['attempt'] > 0 and r['outcome'] == 'ok')
        info['ready_line'] = [(round(s['ts'] - t0, 1), ready_endpoints(s), ready_endpoints(s, 'A'), ready_endpoints(s, 'B'),
                               len([p for p in s['pods'] if not p['terminating']])) for s in tl]
        out[name] = info
    return out


def exp_b():
    runs = {}
    for meta_path in sorted((RES / 'exp_b').glob('*.meta.json')):
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        name = meta['run']
        rows = [r for r in load(RES / 'exp_b' / f'{name}.jsonl') if r['phase'] == 'measure']
        traffic, policy, rep = name.split('-')
        per_pod = {}
        for pod, group in sorted(group_by(rows, 'pod').items(), key=lambda kv: str(kv[0])):
            ok = [r for r in group if r['outcome'] == 'ok']
            per_pod[pod] = {'requests': len(group), 'share': round(len(group) / len(rows), 3),
                            'queue_mean': mean([r['queue_s'] for r in ok if r['queue_s'] is not None]),
                            'queue_p95': pct([r['queue_s'] for r in ok if r['queue_s'] is not None], .95),
                            'ttfc_p50': pct([r['ttfc_s'] for r in ok], .5), 'ttfc_p95': pct([r['ttfc_s'] for r in ok], .95),
                            'hit_ratio': mean([r['cache'] == 'hit' for r in ok]), 'rejected': sum(r['outcome'] == 'rejected' for r in group)}
        client_map = defaultdict(Counter)
        for r in rows:
            client_map[f"c{r['client']} {r.get('client_ip')}"][r.get('pod')] += 1
        scheduled = sum(1 for i in json.loads((RES / 'exp_b' / f'schedule-{traffic}.json').read_text()) if i['phase'] == 'measure')
        runs[name] = {'traffic': traffic, 'policy': policy, 'rep': int(rep[1:]), 'stats': stats(rows, scheduled),
                      'per_pod': per_pod, 'max_pod_share': max(p['share'] for p in per_pod.values()),
                      'client_to_pod': {k: dict(v) for k, v in client_map.items()},
                      'hit_ratio': mean([r['cache'] == 'hit' for r in rows if r['outcome'] == 'ok']),
                      'window': [meta['measure_from'], meta['measure_to']]}
    return runs


def group_by(rows, key):
    out = defaultdict(list)
    for r in rows:
        out[r.get(key)].append(r)
    return out


def mean(values):
    values = [float(v) for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def prom(query, at):
    url = 'http://localhost:9090/api/v1/query?' + urllib.parse.urlencode({'query': query, 'time': at})
    done = subprocess.run([KUBECTL, '-n', 'monitoring', 'exec', 'deploy/prometheus', '--', 'wget', '-qO-', url],
                          capture_output=True, text=True, encoding='utf-8')
    data = json.loads(done.stdout)['data']['result']
    return float(data[0]['value'][1]) if data else None


def promql_check(runs):
    """Перцентиль из суммарных bucket за то же окно, что и клиентский журнал."""
    checks = []
    for name in ('hot80-plain-r1', 'hot80-affinity-r1', 'uniform-plain-r1'):
        if name not in runs:
            continue
        lo, hi = runs[name]['window']
        span = int(hi - lo)
        row = {'run': name, 'window_s': span, 'client_n_ok': runs[name]['stats']['n_ok']}
        for q in (.5, .95):
            expr = f'histogram_quantile({q}, sum by (le) (increase(sim_ttfc_seconds_bucket[{span}s])))'
            row[f'prom_ttfc_p{int(q * 100)}'] = prom(expr, hi)
            row[f'client_ttfc_p{int(q * 100)}'] = runs[name]['stats'][f'ttfc_p{int(q * 100)}']
        row['prom_count'] = prom(f'sum(increase(sim_ttfc_seconds_count[{span}s]))', hi)
        row['promql'] = f'histogram_quantile(0.95, sum by (le) (increase(sim_ttfc_seconds_bucket[{span}s])))'
        checks.append(row)
    return checks


def exp_c():
    d = RES / 'exp_c'
    events = json.loads((d / 'events.json').read_text(encoding='utf-8'))
    t0 = events['start_at']
    rows, tl = load(d / 'requests.jsonl'), timeline(d / 'timeline.jsonl')
    first_seen, first_ready, gone = {}, {}, {}
    for snap in tl:
        names = set()
        for p in snap['pods']:
            names.add(p['name'])
            first_seen.setdefault(p['name'], snap['ts'] - t0)
            if p['ready']:
                first_ready.setdefault(p['name'], snap['ts'] - t0)
            if p['terminating']:
                gone.setdefault(p['name'], snap['ts'] - t0)
    desired3 = next((s['ts'] - t0 for s in tl if (s.get('hpa') or {}).get('desired') == 3), None)
    load_end = events['load_end'] - t0
    back = next((s['ts'] - t0 for s in tl if s['ts'] - t0 > load_end and (s.get('hpa') or {}).get('current') == 2), None)
    phases = {}
    for run, group in group_by(rows, 'run').items():
        phases[run] = {'stats': stats(group), 'per_pod': dict(Counter(r.get('pod') for r in group))}
    alerts = []
    last = {}
    for s in tl:
        for a in s.get('alerts') or []:
            if last.get(a['name']) != a['state']:
                alerts.append({'t': round(s['ts'] - t0, 1), 'alert': a['name'], 'state': a['state']})
                last[a['name']] = a['state']
        for name in list(last):
            if s.get('alerts') is not None and name not in {a['name'] for a in s['alerts']} and last[name] != 'inactive':
                alerts.append({'t': round(s['ts'] - t0, 1), 'alert': name, 'state': 'inactive'})
                last[name] = 'inactive'
    return {'hpa_desired_3_s': desired3, 'pods': {n: {'first_seen_s': round(first_seen[n], 1),
                                                      'ready_s': round(first_ready[n], 1) if n in first_ready else None,
                                                      'terminating_s': round(gone[n], 1) if n in gone else None}
                                                  for n in first_seen},
            'load_end_s': round(load_end, 1), 'back_to_2_s': round(back, 1) if back else None,
            'scale_down_after_load_s': round(back - load_end, 1) if back else None, 'phases': phases, 'alerts': alerts,
            'series': [(round(s['ts'] - t0, 1), (s.get('hpa') or {}).get('current'), (s.get('hpa') or {}).get('desired'),
                        (s.get('hpa') or {}).get('cpu_utilization'), ready_endpoints(s)) for s in tl]}


def figures(a, b, c):
    FIG.mkdir(parents=True, exist_ok=True)
    # 1. Прогрев и rollout: Ready по времени и запросы.
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=False)
    for ax, name, title in ((axes[0], 'warmup-defect', 'Прогрев: исходные probes (/health)'),
                            (axes[1], 'warmup-fixed', 'Прогрев: startup/readiness по /startup и /ready')):
        line = a[name]['ready_line']
        ax.step([x[0] for x in line], [x[1] for x in line], where='post', color='k', label='Ready endpoints')
        rows = load(RES / 'exp_a' / name / 'requests.jsonl')
        t0 = json.loads((RES / 'exp_a' / name / 'summary.json').read_text(encoding='utf-8'))['start_at']
        for warm, color, label in ((True, 'tab:green', 'обслужен прогретым'), (False, 'tab:red', 'обслужен НЕпрогретым')):
            pts = [(r['sent_wall'] - t0, r['ttfc_s']) for r in rows if r.get('warm') is warm and r['ttfc_s']]
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=8, color=color, label=f'TTFC, {label}')
        rej = [r['sent_wall'] - t0 for r in rows if r['outcome'] == 'rejected']
        ax.scatter(rej, [0.05] * len(rej), marker='x', color='tab:orange', label='429')
        ax.set_title(title)
        ax.set_ylabel('реплики / TTFC, с')
        ax.legend(fontsize=8, loc='upper right')
    line = a['rollout-A-to-B']['ready_line']
    ax = axes[2]
    ax.step([x[0] for x in line], [x[2] for x in line], where='post', label='Ready A')
    ax.step([x[0] for x in line], [x[3] for x in line], where='post', label='Ready B')
    for i, s in enumerate(a['rollout-A-to-B']['long_streams']):
        ax.plot([s['sent_s'], s['done_s']], [2.45 - 0.15 * i] * 2, lw=4,
                label=f"поток {s['chunks']} частей, Pod {s['pod'][-5:]} ({s['version']}), {s['outcome']}")
    ax.axvline(12, color='grey', ls='--', label='helm upgrade → B')
    ax.set_title('Rolling update A → B под нагрузкой 3 запр/с')
    ax.set_xlabel('с от начала нагрузки')
    ax.set_ylabel('реплик')
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / '1_warmup_rollout.png', dpi=110)
    plt.close(fig)

    # 2. Задержка и очередь по Pod: горячая реплика.
    fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharey='row')
    for col, policy in enumerate(('affinity', 'plain')):
        name = f'hot80-{policy}-r1'
        rows = [r for r in load(RES / 'exp_b' / f'{name}.jsonl') if r['phase'] == 'measure' and r['outcome'] == 'ok']
        t0 = min(r['planned_wall'] for r in rows)
        for pod, group in sorted(group_by(rows, 'pod').items()):
            axes[0][col].scatter([r['sent_wall'] - t0 for r in group], [r['ttfc_s'] for r in group], s=6, label=pod[-5:])
            axes[1][col].scatter([r['sent_wall'] - t0 for r in group], [r['queue_s'] for r in group], s=6, label=pod[-5:])
        axes[0][col].set_title(f'80% через одного клиента, Service: {policy}')
        axes[0][col].set_ylabel('TTFC, с')
        axes[1][col].set_ylabel('ожидание в очереди, с')
        axes[1][col].set_xlabel('с от начала замера')
        axes[0][col].legend(title='Pod', fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / '2_latency_queue_by_pod.png', dpi=110)
    plt.close(fig)

    # 3. Сравнение политик по трём прогонам.
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    combos = [(t, p) for t in ('uniform', 'hot80') for p in ('affinity', 'plain')]
    for ax, metric, label in ((axes[0], 'ttfc_p95', 'p95 TTFC, с'), (axes[1], 'max_pod_share', 'доля самого загруженного Pod'),
                              (axes[2], 'rejected', 'отказы 429 за замер')):
        for i, (t, p) in enumerate(combos):
            vals = []
            for name, run in b.items():
                if run['traffic'] == t and run['policy'] == p:
                    vals.append(run['stats'][metric] if metric in run['stats'] else run[metric])
            ax.scatter([i] * len(vals), vals, color='k', zorder=3)
            ax.bar(i, sum(vals) / len(vals) if vals else 0, alpha=.4)
        ax.set_xticks(range(len(combos)), [f'{t}\n{p}' for t, p in combos])
        ax.set_title(label)
    fig.suptitle('Политики Service × трафик, 3 прогона (точки), среднее (столбец); замер 120 с, 3,5 запр/с')
    fig.tight_layout()
    fig.savefig(FIG / '3_policy_comparison.png', dpi=110)
    plt.close(fig)

    # 4. HPA 2 → 3 → 2 вместе с нагрузкой.
    fig, ax = plt.subplots(figsize=(11, 4.5))
    s = c['series']
    ax.step([x[0] for x in s], [x[1] for x in s], where='post', label='HPA current')
    ax.step([x[0] for x in s], [x[2] for x in s], where='post', ls='--', label='HPA desired')
    ax.step([x[0] for x in s], [x[4] for x in s], where='post', label='Ready endpoints')
    ax.set_ylabel('реплик')
    ax.set_xlabel('с от начала нагрузки')
    ax2 = ax.twinx()
    ax2.plot([x[0] for x in s], [x[3] for x in s], color='tab:red', alpha=.6, label='CPU, % от requests')
    rows = load(RES / 'exp_c' / 'requests.jsonl')
    t0 = json.loads((RES / 'exp_c' / 'events.json').read_text(encoding='utf-8'))['start_at']
    sec = Counter(int((r['sent_wall'] - t0) // 10) * 10 for r in rows if r.get('attempt', 0) == 0)
    ax2.bar(list(sec), [v / 10 * 100 for v in sec.values()], width=9, alpha=.15, color='grey', label='нагрузка, запр/с ×100')
    ax2.set_ylabel('CPU % / нагрузка')
    ax.axvline(c['load_end_s'], color='k', ls=':', label='нагрузка снята')
    ax.legend(loc='upper left', fontsize=8)
    ax2.legend(loc='upper right', fontsize=8)
    ax.set_title('HPA autoscaling/v2, min 2 / max 3, цель CPU 60% от requests (200m)')
    fig.tight_layout()
    fig.savefig(FIG / '4_hpa_scaling.png', dpi=110)
    plt.close(fig)


def export_csv():
    rows = []
    for path in sorted(RES.rglob('*.jsonl')):
        if path.name in ('timeline.jsonl',) or path.parent.name == 'figures':
            continue
        for r in load(path):
            if 'outcome' in r and 'request_id' in r:
                r = {k: v for k, v in r.items() if k != 'chunk_times'}
                r['source'] = str(path.relative_to(RES)).replace('\\', '/')
                rows.append(r)
    keys = sorted({k for r in rows for k in r})
    with (RES / 'requests.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in r.items()})
    return len(rows)


def main():
    a, b, c = exp_a(), exp_b(), exp_c()
    checks = promql_check(b)
    figures(a, b, c)
    n = export_csv()
    summary = {'exp_a': a, 'exp_b': b, 'exp_c': c, 'promql_check': checks, 'requests_csv_rows': n}
    (RES / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    print('requests.csv строк:', n)


if __name__ == '__main__':
    main()
