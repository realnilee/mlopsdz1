"""Общие функции опытов: kubectl, запуск генератора в клиентских Pod, наблюдатель за стендом."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import loadgen  # noqa: E402

RESULTS = ROOT / 'results'
_WINGET = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'WinGet' / 'Packages'
KUBECTL = os.environ.get('KUBECTL', r'C:\Program Files\Docker\Docker\resources\bin\kubectl.exe')
HELM = os.environ.get('HELM', str(_WINGET / 'Helm.Helm_Microsoft.Winget.Source_8wekyb3d8bbwe' / 'windows-amd64' / 'helm.exe'))
SHA = {'A': 'f99a0b448a959a3f05468d5458af60917615030b6fe8e110f595b15a49d240ac',
       'B': '9434d28c32a4933676145bbb2ff4b8ec4940b20b70fd389644a1924bb04ef9a1'}


def say(*parts):
    print(time.strftime('%H:%M:%S'), *parts, flush=True)


def kubectl(*args, check=True, input=None, timeout=300):
    done = subprocess.run([KUBECTL, *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                          input=input, timeout=timeout)
    if check and done.returncode != 0:
        raise RuntimeError(f'kubectl {" ".join(args)}: {done.stderr.strip()}')
    return done.stdout


def kjson(*args):
    return json.loads(kubectl(*args, '-o', 'json'))


def helm(*args, timeout=300):
    done = subprocess.run([HELM, *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                          cwd=ROOT, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f'helm {" ".join(args)}: {done.stderr.strip()}')
    say('helm', *args[:2], '->', done.stdout.strip().splitlines()[0] if done.stdout.strip() else 'ok')
    return done.stdout


def deploy(version='A', fixed=True, hpa=False, wait=True, extra=()):
    args = ['upgrade', '--install', 'sim', 'chart/sim-api', '--set', f'model.version={version}',
            '--set', f'model.sha256={SHA.get(version, version)}', '--set', f'probes.fixed={str(fixed).lower()}',
            '--set', f'hpa.enabled={str(hpa).lower()}', *extra]
    if wait:
        args += ['--wait', '--timeout', '180s']
    return helm(*args)


def sim_pods():
    return [p for p in kjson('get', 'pods', '-l', 'app=sim-api')['items']]


def ready_sim_pods():
    out = []
    for p in sim_pods():
        if p['metadata'].get('deletionTimestamp'):
            continue
        cond = {c['type']: c['status'] for c in p['status'].get('conditions', [])}
        if cond.get('Ready') == 'True':
            out.append(p)
    return out


def wait_ready(count, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pods = ready_sim_pods()
        if len(pods) == count and len(sim_pods()) == count:
            return pods
        time.sleep(2)
    raise TimeoutError(f'нет {count} готовых Pod')


def clients():
    pods = [p for p in kjson('get', 'pods', '-l', 'app=loadgen')['items'] if not p['metadata'].get('deletionTimestamp')]
    pods.sort(key=lambda p: p['metadata']['name'])
    return [(p['metadata']['name'], p['status'].get('podIP')) for p in pods]


def fresh_clients():
    """Новые клиентские Pod — новые IP: привязка ClientIP начинается с чистого листа."""
    kubectl('rollout', 'restart', 'deploy/loadgen')
    kubectl('rollout', 'status', 'deploy/loadgen', '--timeout=120s')
    for _ in range(60):
        found = clients()
        phases = [kjson('get', 'pod', name)['status']['phase'] for name, _ in found]
        if len(found) == 4 and all(ip for _, ip in found) and all(ph == 'Running' for ph in phases):
            return found
        time.sleep(1)
    raise TimeoutError('клиенты не поднялись')


def put(pod, remote, data):
    kubectl('exec', '-i', pod, '--', 'sh', '-c', f'cat > {remote}', input=data)


def get(pod, remote):
    return kubectl('exec', pod, '--', 'cat', remote, check=False)


def start_load(pod, schedule, url, out, start_at, client=None, retries=0, timeout=90, tags=None):
    # Своё имя файла расписания на каждый запуск: два генератора в одном Pod не должны делить файл.
    path = f'/tmp/schedule-{Path(out).stem}.json'
    put(pod, path, json.dumps(schedule))
    kubectl('exec', pod, '--', 'rm', '-f', out)
    args = ['exec', pod, '--', 'python', '-m', 'app.loadgen', '--schedule', path, '--url', url,
            '--out', out, '--start-at', str(start_at), '--retries', str(retries), '--timeout', str(timeout)]
    if client is not None:
        args += ['--client', str(client)]
    for key, value in (tags or {}).items():
        args += ['--tag', f'{key}={value}']
    return subprocess.Popen([KUBECTL, *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def collect(pod, out):
    return [json.loads(x) for x in get(pod, out).splitlines() if x.strip()]


def pod_get(client, ip, path, method='GET'):
    code = (f"import urllib.request;r=urllib.request.Request('http://{ip}:8000{path}',method='{method}');"
            f"print(urllib.request.urlopen(r,timeout=5).read().decode())")
    return kubectl('exec', client, '--', 'python', '-c', code)


def reset_caches(client):
    for p in ready_sim_pods():
        pod_get(client, p['status']['podIP'], '/admin/reset-cache', 'POST')


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


class Watcher:
    """Каждую секунду пишет Pod (Ready, версия, рестарты) и EndpointSlice сервиса sim;
    для каждого нового Pod сервиса запускает kubectl logs -f, чтобы журнал не пропал при замене."""

    def __init__(self, directory, hpa=False, alerts=False, interval=1.0):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hpa, self.alerts, self.interval = hpa, alerts, interval
        self.stop_flag = threading.Event()
        self.followers = {}
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_flag.set()
        self.thread.join(30)
        time.sleep(2)
        for proc, handle in self.followers.values():
            proc.terminate()
            handle.close()

    def snapshot(self):
        pods = sim_pods()
        slices = kjson('get', 'endpointslices', '-l', 'kubernetes.io/service-name=sim')['items']
        row = {'ts': time.time(), 'pods': [], 'endpoints': []}
        for p in pods:
            cond = {c['type']: c['status'] for c in p['status'].get('conditions', [])}
            cs = (p['status'].get('containerStatuses') or [{}])[0]
            row['pods'].append({'name': p['metadata']['name'], 'version': p['metadata']['labels'].get('model-version'),
                                'phase': p['status'].get('phase'), 'ready': cond.get('Ready') == 'True',
                                'started': cs.get('started'), 'restarts': cs.get('restartCount', 0),
                                'terminating': bool(p['metadata'].get('deletionTimestamp')), 'ip': p['status'].get('podIP')})
            name = p['metadata']['name']
            if name not in self.followers and p['status'].get('phase') == 'Running':
                handle = (self.dir / f'pod-{name}.log').open('a', encoding='utf-8')
                proc = subprocess.Popen([KUBECTL, 'logs', '-f', name], stdout=handle, stderr=subprocess.DEVNULL)
                self.followers[name] = (proc, handle)
        for s in slices:
            for e in s.get('endpoints') or []:
                c = e.get('conditions', {})
                row['endpoints'].append({'pod': (e.get('targetRef') or {}).get('name'), 'ready': c.get('ready'),
                                         'serving': c.get('serving'), 'terminating': c.get('terminating')})
        if self.hpa:
            hpa = kjson('get', 'hpa', 'sim')
            st = hpa.get('status', {})
            metric = (st.get('currentMetrics') or [{}])[0].get('resource', {}).get('current', {})
            row['hpa'] = {'current': st.get('currentReplicas'), 'desired': st.get('desiredReplicas'),
                          'cpu_utilization': metric.get('averageUtilization')}
        if self.alerts:
            prom = kubectl('-n', 'monitoring', 'exec', 'deploy/prometheus', '--', 'wget', '-qO-',
                           'http://localhost:9090/api/v1/alerts', check=False)
            try:
                row['alerts'] = [{'name': a['labels']['alertname'], 'state': a['state'], 'since': a.get('activeAt')}
                                 for a in json.loads(prom)['data']['alerts']]
            except (ValueError, KeyError):
                row['alerts'] = None
        return row

    def loop(self):
        with (self.dir / 'timeline.jsonl').open('a', encoding='utf-8') as f:
            while not self.stop_flag.is_set():
                started = time.time()
                try:
                    f.write(json.dumps(self.snapshot(), ensure_ascii=False) + '\n')
                    f.flush()
                except Exception as error:  # наблюдатель не должен ронять опыт
                    f.write(json.dumps({'ts': time.time(), 'watch_error': str(error)[:300]}) + '\n')
                time.sleep(max(0.0, self.interval - (time.time() - started)))


def outcome_counts(rows):
    counts = {}
    for row in rows:
        counts[row['outcome']] = counts.get(row['outcome'], 0) + 1
    return counts
