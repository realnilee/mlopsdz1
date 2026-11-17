"""Проверки сервиса в Kubernetes. Запросы идут из клиентского Pod через Service.

HW11_SKIP_DEPLOY=1 — не переустанавливать чарт (для повтора на исходной конфигурации
probes: helm upgrade ... --set probes.fixed=false, затем этот тест должен упасть).
"""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experiments'))
from kube import SHA, clients, collect, deploy, helm, kjson, kubectl, loadgen, start_load, wait_ready  # noqa: E402

URL = 'http://sim:8000/generate'
WARMUP = 20

DIALOG = '''
import json,sys
from app import loadgen
payload=json.loads(sys.argv[1])
row=loadgen.request_once("http://sim:8000/generate",payload,payload.pop("rid"),30)
print(json.dumps(row))
'''


def ask(client, prompt, history=(), session='', rid='t'):
    payload = json.dumps({'prompt': prompt, 'history': list(history), 'session_id': session, 'max_new_tokens': 3,
                          'rid': rid})
    return json.loads(kubectl('exec', client, '--', 'python', '-c', DIALOG, payload).strip().splitlines()[-1])


def info(client, ip):
    code = f"import urllib.request;print(urllib.request.urlopen('http://{ip}:8000/info',timeout=5).read().decode())"
    return json.loads(kubectl('exec', client, '--', 'python', '-c', code))


def ready_since(pod):
    """Секунды от старта контейнера до перехода Ready."""
    status = kjson('get', 'pod', pod)['status']
    started = status['containerStatuses'][0]['state']['running']['startedAt']
    ready = next(c['lastTransitionTime'] for c in status['conditions'] if c['type'] == 'Ready')
    parse = lambda s: time.mktime(time.strptime(s, '%Y-%m-%dT%H:%M:%SZ'))
    return parse(ready) - parse(started)


class K8sTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('HW11_SKIP_DEPLOY') != '1':
            deploy('A', fixed=True)
        wait_ready(2)
        cls.client = clients()[0][0]

    def test_1_new_pod_gets_no_traffic_before_warmup(self):
        items = loadgen.make_schedule(3, 50, 1, sessions_per_client=20, seed=7)
        start_at = time.time() + 4
        proc = start_load(self.client, items, URL, '/tmp/t1.jsonl', start_at, tags={'run': 'test1'})
        time.sleep(7)
        old = {p['metadata']['name'] for p in kjson('get', 'pods', '-l', 'app=sim-api')['items']}
        kubectl('delete', 'pod', sorted(old)[0], '--wait=false')
        proc.wait(timeout=180)
        rows = collect(self.client, '/tmp/t1.jsonl')
        cold = [r for r in rows if r.get('warm') is False]
        self.assertEqual(cold, [], f'{len(cold)} запросов обслужены непрогретым Pod')
        self.assertTrue(all(r['outcome'] == 'ok' for r in rows), {r['outcome'] for r in rows})
        pods = wait_ready(2)
        new = [p['metadata']['name'] for p in pods if p['metadata']['name'] not in old][0]
        self.assertGreaterEqual(ready_since(new), WARMUP - 1, 'Ready раньше конца прогрева')

    def test_2_version_after_rollback_is_confirmed_by_answers(self):
        before = max(h['revision'] for h in json.loads(helm('history', 'sim', '-o', 'json')))
        deploy('B', fixed=True)
        self.assertEqual({ask(self.client, 'q')['version'] for _ in range(4)}, {'B'})
        helm('rollback', 'sim', str(before), '--wait', '--timeout', '180s')
        pods = wait_ready(2)
        answers = [ask(self.client, 'q', rid=f'rb{i}') for i in range(8)]
        self.assertEqual({a['version'] for a in answers}, {'A'})
        for p in pods:
            data = info(self.client, p['status']['podIP'])
            self.assertEqual((data['version'], data['model_sha256']), ('A', SHA['A']))

    def test_3_replaced_pod_restores_version_and_continues_dialog(self):
        first = ask(self.client, 'Запомни CTRL-gamma', session='dlg-g', rid='d1')
        self.assertEqual(first['outcome'], 'ok')
        kubectl('delete', 'pod', first['pod'], '--wait=true', timeout=120)
        pods = wait_ready(2)
        self.assertNotIn(first['pod'], [p['metadata']['name'] for p in pods])
        for p in pods:
            data = info(self.client, p['status']['podIP'])
            self.assertEqual((data['version'], data['model_sha256']), ('A', SHA['A']))
        second = ask(self.client, 'Что я просил запомнить?', history=['Запомни CTRL-gamma', 'tok0 tok1 tok2'],
                     session='dlg-g', rid='d2')
        self.assertEqual(second['outcome'], 'ok')
        self.assertNotEqual(second['pod'], first['pod'])
        self.assertEqual(second['echo'], ['CTRL-gamma'], 'контекст диалога пришёл с запросом, а не из Pod')

    def test_4_two_dialogs_stay_isolated(self):
        results = {}

        def dialog(marker):
            history = []
            for turn in range(3):
                prompt = f'Ход {turn}, контроль {marker}' if turn == 0 else f'Ход {turn}'
                row = ask(self.client, prompt, history, session=marker, rid=f'{marker}-{turn}')
                results.setdefault(marker, []).append(row)
                history += [prompt, 'tok0 tok1 tok2']
        threads = [threading.Thread(target=dialog, args=(m,)) for m in ('CTRL-alpha', 'CTRL-beta')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for marker, rows in results.items():
            for row in rows:
                self.assertEqual(row['outcome'], 'ok')
                self.assertEqual(row['echo'], [marker], f'чужой контекст в ответе {row["request_id"]}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
