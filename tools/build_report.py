"""report.html из results/summary.json и сырых файлов. Все числа берутся из данных прогона."""
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / 'results'
S = json.loads((RES / 'summary.json').read_text(encoding='utf-8'))
A, B, C = S['exp_a'], S['exp_b'], S['exp_c']
GPU = json.loads((RES / 'real_model' / 'summary.json').read_text(encoding='utf-8'))
NET = json.loads((RES / 'exp_b' / 'network.json').read_text(encoding='utf-8'))


def f(x, d=2):
    return '—' if x is None else (f'{x:.{d}f}'.replace('.', ',') if isinstance(x, float) else str(x))


def table(head, rows):
    out = ['<table><thead><tr>' + ''.join(f'<th>{html.escape(h)}</th>' for h in head) + '</tr></thead><tbody>']
    for r in rows:
        out.append('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>')
    return '\n'.join(out + ['</tbody></table>'])


def stat_row(name, st, extra=()):
    return [name, st['scheduled'], st['sent'], st['completed'], st['rejected'], st['aborted'], st['timeout'],
            st['http_5xx'] + st['connect_error'], f(st['error_share'] * 100, 1) + '%', st['n_ok'],
            f(st['ttfc_p50']), f(st['ttfc_p95']), *extra]


STAT_HEAD = ['Опыт', 'Запланировано', 'Отправлено', 'Завершено', 'Отклонено 429', 'Оборвано', 'Просрочено',
             '5xx/нет соединения', 'Доля отказов', 'n успешных', 'TTFC p50, с', 'TTFC p95, с']

wd, wf, ro, br, fk = (A[k] for k in ('warmup-defect', 'warmup-fixed', 'rollout-A-to-B', 'broken-B-and-rollback', 'forced-kill'))
new_defect = [p for p in wd['pods'].values() if p['first_seen_s'] > 0][0]
new_fixed = [p for p in wf['pods'].values() if p['first_seen_s'] > 0][0]
long_rows = [[f(s['sent_s'], 1), f(s['done_s'], 1), s['chunks'], s['pod'], s['version'], s['outcome']] for s in ro['long_streams']]

runs = sorted(B.values(), key=lambda r: (r['traffic'], r['policy'], r['rep']))
b_rows = []
for r in runs:
    st = r['stats']
    pods = ' / '.join(str(p['requests']) for p in r['per_pod'].values())
    b_rows.append([r['traffic'], r['policy'], r['rep'], st['scheduled'], st['completed'], st['rejected'], pods,
                   f(r['max_pod_share'], 3), f(r['hit_ratio'], 2), f(st['ttfc_p50']), f(st['ttfc_p95']),
                   f(st.get('send_lag_max'), 4)])


def combo(traffic, policy, key):
    vals = [r['stats'][key] if key in r['stats'] else r[key] for r in runs if r['traffic'] == traffic and r['policy'] == policy]
    return f'{f(min(vals))}–{f(max(vals))}'


trace_hot = B['hot80-affinity-r2']
trace_even = B['uniform-affinity-r3']
trace_skew = B['uniform-affinity-r1']


def client_map(run):
    return '<br>'.join(f'{html.escape(k)} → ' + ', '.join(f'{p[-5:]}: {n}' for p, n in v.items())
                       for k, v in sorted(run['client_to_pod'].items()))


pod_rows = []
for name in ('hot80-affinity-r1', 'hot80-plain-r1', 'hot80-cacheaware-r1', 'uniform-affinity-r1', 'uniform-plain-r1', 'uniform-cacheaware-r1'):
    for pod, p in B[name]['per_pod'].items():
        pod_rows.append([name, pod, p['requests'], f(p['queue_mean']), f(p['queue_p95']), f(p['ttfc_p50']), f(p['ttfc_p95']),
                         f(p['hit_ratio'], 2), p['rejected']])

hpa_pods = sorted(C['pods'].items(), key=lambda kv: kv[1]['first_seen_s'])
new_pod = [n for n, p in hpa_pods if p['first_seen_s'] > 0][0]
ph = C['phases']
alert_rows = [[f(a['t'], 1), a['alert'], a['state']] for a in C['alerts']]
prom_rows = [[c['run'], c['client_n_ok'], f(c['prom_count'], 1), f(c['client_ttfc_p50']), f(c['prom_ttfc_p50']),
              f(c['client_ttfc_p95']), f(c['prom_ttfc_p95'])] for c in S['promql_check']]
g1, g2 = GPU['per_concurrency']['1'], GPU['per_concurrency']['2']
checks_red = (RES / 'checks' / 'red_defect_probes.log').read_text(encoding='utf-8')
red_count = checks_red.split(' запросов обслужены непрогретым Pod')[0].split()[-1]

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#5b5b63;--line:#d9d6cf;--card:#f1efe9;--accent:#7a3e9d}
@media (prefers-color-scheme: dark){:root{--bg:#18181b;--fg:#ececf0;--muted:#a7a7b2;--line:#3a3a42;--card:#222228;--accent:#c79bff}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif;margin:0}
main{max-width:1100px;margin:auto;padding:32px 16px 80px}
h1{font-size:30px;line-height:1.2}h2{margin-top:44px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{margin-top:28px}code{font-size:.9em;background:var(--card);padding:1px 4px;border-radius:3px;overflow-wrap:anywhere}
pre{background:var(--card);padding:12px;overflow-x:auto;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:12px 0;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.note{border-left:3px solid var(--accent);padding:8px 14px;background:var(--card)}
img{max-width:100%;border:1px solid var(--line);background:#fff}.muted{color:var(--muted)}
dl{display:grid;grid-template-columns:200px 1fr;gap:4px 16px}dt{color:var(--muted)}
"""

body = f"""
<h1>ДЗ 11. Сервис работает, пользователи ждут</h1>
<p class="muted">MLOPS-26 · сервис ДЗ 8+9+10 в Kubernetes (kind) · основная часть на имитаторе <code>backend_mode=simulated</code>,
короткий опыт с моделью ДЗ 6+7 на GPU — отдельно, в разделе 6.</p>
<div class="note">Все числа ниже взяты из сырых журналов <code>results/</code> (<code>requests.csv</code>, {S['requests_csv_rows']} строк,
и JSONL по опытам). Числа имитатора описывают модель нагрузки имитатора, а не скорость языковой модели.</div>

<h2>1. Стенд</h2>
<dl>
<dt>Окружение</dt><dd>Windows 11, Docker Desktop 29.9.2 на WSL2: 16 vCPU, 15,5 ГиБ. kind v0.33.0, узел <code>kindest/node:v1.37.0</code>
(containerd 2.3.4), kubectl v1.36.1, Helm v4.3.2. Свободное место до начала: 69 ГБ.</dd>
<dt>Сервис</dt><dd>Helm-чарт <code>chart/sim-api</code>: Deployment с 2 репликами, ConfigMap, два Service (обычный и с <code>ClientIP</code>), HPA по флагу.
В values параметризованы образ, версия модели и её SHA-256, прогрев, число реплик, режим probes и параметры backend.
Образ <code>sim-api:0.1.0</code> собран на <code>python:3.12.12-slim</code> с закреплёнными зависимостями: 202 МБ, в узле 49 МБ.</dd>
<dt>Ресурсы Pod</dt><dd>requests 200m CPU и 128 МиБ, limits 1 CPU и 256 МиБ. Стратегия <code>maxSurge 1, maxUnavailable 0</code>,
<code>terminationGracePeriodSeconds 60</code>, preStop <code>/drain</code> 5 с, graceful shutdown uvicorn 45 с.</dd>
<dt>Хранение метрик</dt><dd>Prometheus v3.1.0: хранение 6 ч, не более 800 МБ, emptyDir с лимитом 1 ГиБ. Grafana 11.4.0 без постоянного тома.
Сбор с каждого Pod через <code>kubernetes_sd_configs: role pod</code>, интервал 5 с; добавлены kube-state-metrics v2.13.0 и metrics-server v0.7.2.</dd>
<dt>Размер стенда</dt><dd>Узел kind: образ около 1 ГБ и том около 2,3 ГБ; образ сервиса 0,2 ГБ. Удаление: <code>kind delete cluster --name hw11</code>
и <code>docker image rm sim-api:0.1.0 kindest/node:v1.37.0</code>. Окружение <code>.venv</code> для GPU-опыта (несколько ГБ) удаляется отдельно.</dd>
<dt>Артефакт</dt><dd>Файлы <code>/models/model-A.json</code> и <code>model-B.json</code> вшиты в образ, неизменяемый по digest. Ожидаемый SHA-256 передаётся из values.
При несовпадении прогрев не завершается, <code>/startup</code> не даёт 200, и Pod не попадает в Ready.
После замены Pod та же версия и тот же хеш подтверждены проверкой 3.</dd>
<dt>Идентификация</dt><dd>Каждый ответ и строка журнала несут <code>request_id</code> (из заголовка клиента), имя Pod, версию, хеш и <code>backend_mode</code>.
История диалога передаётся в запросе, Pod её не хранит. Продолжение диалога после замены Pod проверено проверкой 3.</dd>
</dl>

<h3>Backend и генератор</h3>
<p>Имитатор: прогрев <b>20 с</b>; не больше <b>2</b> одновременных вычислений на Pod; очередь до 32, сверх неё 429.
Ответ выдаётся частями NDJSON, пауза между частями 0,05 с. Префиксный кеш — LRU на <b>16</b> префиксов в каждом Pod.
Стоимость prefill: <b>hit 0,25 с, miss 0,6 с</b> вычислений на CPU. Непрогретый Pod добавляет 2 с.
Ключ кеша — SHA-256 от prefix и истории.</p>
<p>Генератор: открытая модель, запросы уходят по расписанию и не ждут ответа. Каждый запрос — новое TCP-соединение (<code>Connection: close</code>).
Время на клиенте измеряется через <code>time.monotonic</code>, сверка запланированного и фактического момента отправки — через <code>time.time</code>.
Сохраняются отправка, первая часть, завершение, статус, Pod, версия, число частей, очередь и попытка.
Повтор — отдельная строка с тем же <code>logical_id</code>.</p>

<h2>2. Опыт A. Прогрев, обновление и откат</h2>
<h3>Прогрев: дефект и исправление</h3>
<p><b>Гипотеза.</b> Если startup и readiness смотрят на <code>/health</code>, Pod объявляется готовым, как только поднялся HTTP.
Тогда Service отправляет рабочий трафик в непрогретый backend.</p>
<p><b>Воспроизведение.</b> Одно и то же расписание: 4 запроса/с, 60 с, один клиентский Pod через Service <code>sim</code>.
На 8-й секунде удаляется один Pod. Отличается только <code>probes.fixed</code>.</p>
{table(STAT_HEAD + ['Обслужено непрогретым'], [stat_row('исходные probes (/health)', wd['stats'], [wd['cold_served']]),
                                                stat_row('исправленные probes', wf['stats'], [wf['cold_served']])])}
<p><b>Данные до.</b> Новый Pod стал Ready через {f(new_defect['ready_s'] - new_defect['first_seen_s'], 1)} с после появления,
хотя прогрев длится 20 с. Непрогретые Pod обслужили {wd['cold_served']} запросов с {f(wd['cold_window_s'][0], 1)} по {f(wd['cold_window_s'][1], 1)} с
(TTFC p50 этих запросов — {f(wd['cold_ttfc_p50'], 1)} с), и {wd['stats']['rejected']} запросов получили 429.
Непрогретые запросы есть уже с нулевой секунды: <code>helm upgrade --wait</code> при исходных probes счёл свежие Pod готовыми.
Значит, дефект ломает и сам выпуск, а не только замену Pod.</p>
<p><b>Причина.</b> <code>/health</code> отвечает о жизни процесса, а не о готовности модели; все три probes делили этот один маршрут.</p>
<p><b>Изменение.</b> У каждой проверки свой маршрут:
<code>startupProbe → /startup</code> (200 только после прогрева, <code>failureThreshold 25 × 2 с = 50 с</code> покрывают 20 с прогрева с запасом);
<code>readinessProbe → /ready</code> (прогрет и не в drain); <code>livenessProbe → /live</code> (event loop отвечает, от прогрева не зависит).
Пока startup не прошёл, liveness и readiness не выполняются.</p>
<p><b>Повтор.</b> Новый Pod стал Ready через {f(new_fixed['ready_s'] - new_fixed['first_seen_s'], 1)} с, непрогретых ответов 0, отказов 0.
Линия Ready в EndpointSlice — на графике 1.
Собственная проверка 1 на исходных probes падает ({red_count} запросов обслужены непрогретым Pod, <code>results/checks/red_defect_probes.log</code>)
и проходит на исправленных (<code>results/checks/green_all.log</code>).</p>
<p><b>Ограничения.</b> Пока второй Pod прогревался, один Pod держал весь поток, и TTFC p95 вырос до {f(wf['stats']['ttfc_p95'], 1)} с.
Ёмкость при замене Pod — отдельный вопрос: его решают запас реплик или <code>maxSurge</code>, а не probes.</p>

<h3>Rolling update A → B под длинными потоками</h3>
<p>Нагрузка 3 запроса/с, 120 с. На 5-й и 20-й секунде запущены два длинных потока: 1000 и 600 частей (50 и 30 с).
На 12-й секунде запущен <code>helm upgrade</code> на B.</p>
{table(STAT_HEAD + ['Версии в ответах'], [stat_row('rollout A→B', ro['stats'], [', '.join(f'{k}: {v}' for k, v in ro['versions'].items())])])}
{table(['Отправлен, с', 'Завершён, с', 'Частей', 'Pod', 'Версия', 'Итог'], long_rows)}
<p>Ни одного 5xx и ни одного оборванного потока. Число Ready не опускалось ниже 2: B1 стал Ready на 35,7 с, после чего ушёл первый A;
B2 стал Ready на 59,0 с. Оба длинных потока попали на один старый Pod. Журнал этого Pod на остановке:
<code>drain running=2 waiting=25</code>. Он доработал все 27 запросов и вышел через 27,7 с, в пределах 45 с graceful shutdown и 60 с grace period.</p>
<p><b>Ограничение.</b> Длинные потоки занимали оба слота вычисления старого Pod, и короткие запросы ждали за ними в очереди.
Поэтому TTFC p95 всего прогона — {f(ro['stats']['ttfc_p95'], 1)} с. Без отказов, но это ожидание. Лечится отдельным лимитом на длинные ответы или выделенным пулом.</p>

<h3>Неготовая B и откат</h3>
<p>Выпущена B с неверным SHA-256. Её Pod <code>{br['broken_pods'][0]}</code> не прошёл startup и перезапускался; в Ready EndpointSlice он не попал
ни разу (записей: {br['broken_in_ready_endpoints']}). Rollout остановился, рабочие B продолжали обслуживать трафик.
Версии в ответах за первые 60 с: {dict(br['versions_first_60s'])}. Затем выполнен <code>helm rollback</code> к ревизии с A и исправными probes.
Последние 20 ответов прогона: {dict(br['versions_last_20'])}. Всего {br['stats']['completed']} из {br['stats']['scheduled']} успешны, ошибок 0.
Версия после отката подтверждена и ответами, и <code>/info</code> каждого Pod в проверке 2.</p>

<h3>Принудительное завершение</h3>
<p><code>kubectl delete pod --grace-period=0 --force</code> на 15-й секунде, нагрузка 3 запроса/с и два потока по 400 частей.
У клиента включено до 2 повторов при обрыве или ошибке соединения.</p>
{table(STAT_HEAD + ['Попыток'], [stat_row('forced kill', fk['stats'], [fk['stats']['attempts']])])}
<p>Оборван {fk['stats']['aborted']} поток: он успел отдать 216 частей из 400. Повтор выполнен с нуля отдельной попыткой и завершился успешно
(повторено логических запросов: {fk['retried_logical']}, успешных повторов: {fk['retried_then_ok']}).
Отказ обнаружен быстро: Pod удалён на 15,0 с, Endpoint исчез из Ready на 15,7 с (наблюдатель опрашивает раз в секунду).
Обслуживание полностью восстановилось на 37,8 с, когда прогрелась замена. Пока работал один Pod, {fk['stats']['rejected']} запросов получили 429
с 32,9 по 44,6 с. Ошибки 5xx и потерянные соединения: 0.</p>

<h2>3. Опыт B. Привязка к Pod и горячая реплика</h2>
<p><b>Гипотеза.</b> Привязка <code>ClientIP</code> сохраняет локальность префиксного кеша, но при неравномерных клиентах создаёт горячую реплику:
она ждёт, а у другой остаётся свободная ёмкость. Обычный Service убирает перекос, но теряет кеш. Маршрутизация по оценке «очередь + prefill с учётом кеша» должна взять лучшее из обоих.</p>
<p><b>Настройки сети.</b> kube-proxy в режиме <code>{NET['kube_proxy_mode'][0].split(':')[1].strip()}</code>. Service <code>sim</code>: ClusterIP {NET['services']['sim']['clusterIP']}, без привязки.
<code>sim-affinity</code>: ClusterIP {NET['services']['sim-affinity']['clusterIP']}, <code>ClientIP</code>, timeout 10 800 с.
Трафик идёт Pod→ClusterIP внутри узла без SNAT, поэтому механизм привязки видит IP клиентского Pod; сервер записывает тот же <code>client_ip</code>.
Keep-alive выключен: каждое соединение выбирает Pod заново, а при <code>ClientIP</code> — по записи привязки.</p>
<p><b>Сценарий.</b> 4 клиентских Pod с разными IP. Пуассоновский поток 3,5 запроса/с, 10 сессий на клиента, prefix 2000 символов, 6 частей ответа.
Расписание общее для всех политик (seed 2026). Трафик двух видов: равномерный и 80% через клиента 0.
Перед каждым прогоном клиентские Pod пересоздаются (новые IP, чистая привязка) и кеш сбрасывается.
10 с прогрева не учитываются, замер длится 120 с. Каждая комбинация выполнена 3 раза: 3 политики × 2 вида трафика × 3 = 18 прогонов.
Запланированное и фактическое время отправки хранятся раздельно; максимальное отставание генератора — {f(max(r['stats'].get('send_lag_max') or 0 for r in runs) * 1000, 1)} мс.</p>
{table(['Трафик', 'Политика', 'Прогон', 'Запланировано', 'Завершено', '429', 'Запросы по Pod', 'Доля самого загруженного Pod', 'Попадания в кеш', 'TTFC p50, с', 'TTFC p95, с', 'Макс. отставание отправки, с'], b_rows)}
<p><b>Очередь и TTFC по репликам</b> (первый прогон каждой комбинации):</p>
{table(['Прогон', 'Pod', 'Запросов', 'Очередь, ср., с', 'Очередь p95, с', 'TTFC p50, с', 'TTFC p95, с', 'Попадания', '429'], pod_rows)}
<p><b>Горячая реплика ждёт, у другой свободно.</b> В прогоне <code>hot80-affinity-r2</code> все 4 клиента закрепились за одним Pod:</p>
<p class="muted">{client_map(trace_hot)}</p>
<p>Один Pod получил все {trace_hot['stats']['scheduled']} запросов. Очередь в среднем {f(trace_hot['per_pod'][next(iter(trace_hot['per_pod']))]['queue_mean'])} с,
TTFC p95 {f(trace_hot['stats']['ttfc_p95'])} с, отказов 429 — {trace_hot['stats']['rejected']}. Второй Pod за это время не обработал ни одного запроса.</p>
<p><b>Перекос, которого ждали, но не было.</b> В <code>uniform-affinity-r3</code> клиенты разошлись 2:2, и нагрузка поделилась поровну:</p>
<p class="muted">{client_map(trace_even)}</p>
<p>Для сравнения <code>uniform-affinity-r1</code> при тех же равномерных клиентах разложился 3:1:</p>
<p class="muted">{client_map(trace_skew)}</p>
<p>Трасса объясняет разницу. Каждый клиент целиком сидит на одном Pod. Распределение задаёт не трафик, а то, куда при первом соединении попал каждый из четырёх IP:
kube-proxy выбирает endpoint случайно, затем закрепляет. При четырёх клиентах раскладка 2:2 выпадает с вероятностью 3/8, 3:1 — 1/2, 4:0 — 1/8.
Фактически в 6 прогонах с привязкой: на равномерном трафике две раскладки 3:1 и одна 2:2, на горячем — две 2:2 и одна 4:0.
На горячем трафике даже раскладка 2:2 оставляет 87% нагрузки одному Pod: клиент 0 сам даёт 80%.</p>
<p><b>Третья политика: cache-aware маршрутизация с учётом очереди</b> (как в лекции: round-robin, привязка, cache-aware).
<code>app/router.py</code> — отдельный Deployment и Service <code>sim-router</code> над теми же двумя Pod. Список Pod берётся из DNS headless Service,
поэтому в нём только Ready-адреса. Для каждого Pod считается оценка времени до первой части:
<code>max(0, в работе + 1 − 2) / 2 × S + (0,25 с, если префикс в индексе Pod, иначе 0,6 с)</code>,
где S — сглаженная длительность ответа этого Pod, измеренная маршрутизатором. Индекс префиксов — LRU на 16 ключей на Pod, как кеш имитатора.
Выбирается Pod с минимальной оценкой. Pod с кешем побеждает, только пока его очередь не съела выгоду в 0,35 с.
Допуск ограничен 18 запросами в работе на Pod, сверх этого маршрутизатор отвечает 429.
Стоимости hit и miss взяты из конфигурации имитатора; на настоящем engine их нужно измерять.
Прогоны этой политики выполнены после остальных на других экземплярах Pod (прежние заменили собственные проверки),
но с той же версией, образом сервиса <code>sim-api:0.1.0</code>, конфигурацией и расписанием.</p>
<p><b>Выбор исправления: cache-aware маршрутизатор.</b></p>
{table(['Трафик', 'Политика', 'TTFC p95 по 3 прогонам, с', 'Доля самого загруженного Pod', 'Попадания в кеш', 'Отказы 429'],
       [[t, p, combo(t, p, 'ttfc_p95'), combo(t, p, 'max_pod_share'), combo(t, p, 'hit_ratio'), combo(t, p, 'rejected')]
        for t in ('uniform', 'hot80') for p in ('affinity', 'plain', 'cacheaware')])}
<p><b>Защита по задержкам, отказам и распределению.</b> Cache-aware маршрутизатор лучший на обоих видах трафика во всех трёх прогонах:
p95 TTFC 1,2–1,6 с на равномерном трафике (обычный Service 2,6–3,1 с, привязка 1,4–12,6 с)
и 0,73–0,77 с на горячем (обычный Service 1,0–1,4 с, привязка 2,5–11,3 с). Отказов 429 нет, доля самого загруженного Pod 0,51–0,52.
Разброс между прогонами минимальный: результат не зависит от случайной раскладки клиентов, в отличие от привязки.</p>
<p><b>Как учитывается локальность кеша.</b> Обычный Service теряет её: на равномерном трафике каждый Pod видит все 40 сессий при кеше на 16,
и попаданий всего 36–38%. Привязка сохраняет локальность (60–93%), но только ценой горячей реплики.
Маршрутизатор держит 52–58% попаданий на равномерном трафике и 84–87% на горячем. Повторный префикс идёт туда, где он уже посчитан,
пока очередь этого Pod не дороже пересчёта. Трасса горячего прогона: клиент 0 (80% трафика) разделён между Pod 161 / 177,
то есть горячий клиент не прибит к одной реплике. Это то же правило, что у scorer в кейсе Google из лекции (раздел 9).</p>
<p><b>Что требует отдельной проверки на настоящем inference engine.</b> Стоимость промаха там зависит от длины префикса и батча,
а не задана константой. Кеш ограничен памятью KV, а не числом записей, и индекс маршрутизатора лишь приближает фактическое состояние кеша.
Непрерывный батчинг меняет связь «в работе — ожидание», поэтому формулу оценки нужно калибровать по метрикам engine (running, waiting, KV usage).
Маршрутизатор в одном экземпляре — единая точка отказа; в рабочем варианте нужны 2+ реплики с общим или согласованным индексом.</p>

<h2>4. Опыт C. HPA и мониторинг</h2>
<p><b>Конфигурация.</b> <code>autoscaling/v2</code>, min 2, max 3, цель — 60% CPU от requests (200m).
Повышение: окно 0 с, не больше 1 Pod за 15 с. Понижение: окно стабилизации 60 с, не больше 1 Pod за 15 с.
Resource metrics API работает (<code>v1beta1.metrics.k8s.io</code>, <code>results/exp_c/metrics-api.txt</code>), у контейнеров сервиса заданы CPU requests.
Нагрузка вычислительная: 5000 сессий на клиента, поэтому почти все запросы — промахи по 0,6 с CPU.</p>
<p><b>Ход.</b> Фаза 1 (0–150 с): 3 запроса/с, 80% через клиента 0, Service с <code>ClientIP</code>. Фаза 2 (150–240 с): то же через обычный Service. Затем нагрузка снята.</p>
{table(['Событие', 'Время, с от начала нагрузки'],
       [['HPA решил: 3 реплики', f(C['hpa_desired_3_s'], 1)], [f'Создан Pod {new_pod}', f(C['pods'][new_pod]['first_seen_s'], 1)],
        ['Новый Pod Ready (после прогрева 20 с)', f(C['pods'][new_pod]['ready_s'], 1)], ['Нагрузка снята', f(C['load_end_s'], 1)],
        ['Pod начал завершаться (3 → 2)', f(C['pods'][new_pod]['terminating_s'], 1)], ['HPA current = 2', f(C['back_to_2_s'], 1)],
        ['Возврат к минимуму после снятия нагрузки', f(C['scale_down_after_load_s'], 1) + ' с (окно 60 с + метрики раз в 15 с)']])}
{table(['Фаза', 'Запросы по Pod', 'Завершено', '429', 'TTFC p50, с', 'TTFC p95, с'],
       [[k, ', '.join(f'{p[-5:]}: {n}' for p, n in v['per_pod'].items()), v['stats']['completed'], v['stats']['rejected'],
         f(v['stats']['ttfc_p50']), f(v['stats']['ttfc_p95'])] for k, v in ph.items()])}
<p><b>Повторная проверка горячей реплики.</b> В фазе 1 все клиенты закрепились за одним Pod: он перегружен, 429 и TTFC p50 около 16 с, второй простаивает.
Средний CPU = (около 500% у горячего + около 0% у холодного) / 2 ≈ 250%, что выше цели 60%. HPA добавил третью реплику, но она за фазу 1
не получила ни одного запроса: записи привязки уже указывают на старый Pod, а новых IP клиентов нет.</p>
<p>Средняя метрика вызывает масштабирование, когда суммарная загрузка всех реплик, делённая на их число, выше цели.
Горячей реплике для этого достаточно быть загруженной сильнее цели в N раз, как здесь. Но масштабирование не лечит горячую точку,
пока трафик пришит к ней привязкой: новая реплика получает только новых клиентов. Фаза 2 через обычный Service распределила запросы
по трём Pod, и отказов не стало.</p>
<h3>Тревоги</h3>
<pre>{html.escape((ROOT / 'monitoring' / 'rules.yml').read_text(encoding='utf-8'))}</pre>
<p>Минимальный объём наблюдений — 30 ответов за минуту, иначе один медленный запрос в тихую минуту поднимал бы тревогу.
Условие должно держаться 30 с (<code>for</code>), вычисление раз в 5 с. Переходы по журналу <code>/api/v1/alerts</code> в той же нагрузке:</p>
{table(['Время, с', 'Тревога', 'Состояние'], alert_rows)}

<h2>5. Метрики и перцентили</h2>
<p>Клиент считает TTFC от отправки до первой части ответа, а полную длительность — до последнего байта.
Сервер пишет Histogram <code>sim_ttfc_seconds</code>, <code>sim_request_duration_seconds</code> и <code>sim_queue_wait_seconds</code>
с границами от 0,05 до 30 с. Метки ограничены: Pod и версия добавляются через discovery, <code>outcome</code> — из 6 значений.
Request ID и session ID пишутся только в журнал.</p>
<p>Перцентиль считается из суммарных bucket за то же окно 120 с, что и клиентский журнал:</p>
<pre>histogram_quantile(0.95, sum by (le) (increase(sim_ttfc_seconds_bucket[120s])))</pre>
{table(['Прогон', 'n в журнале клиента', 'n по Prometheus', 'p50 клиент, с', 'p50 Prometheus, с', 'p95 клиент, с', 'p95 Prometheus, с'], prom_rows)}
<p>Расхождения ожидаемы. Сервер отсчитывает TTFC от приёма запроса, без установки соединения. Перцентиль из classic histogram
линейно интерполируется внутри bucket: для p50 это bucket 0,25–0,5 с, поэтому значение грубое. <code>increase()</code> экстраполирует края окна,
отсюда дробное n. p95 совпадает с точностью до 0,05–0,2 с.</p>
<p>Дашборд: <code>monitoring/grafana-dashboard.json</code> (провиженинг) и живой экспорт из Grafana <code>monitoring/grafana-dashboard-export.json</code>.
Панели: TTFC p50/p95 по Pod, полная длительность, running/waiting по Pod, завершённые запросы/с, ошибки и отказы по исходу, CPU и RAM процесса, Ready-реплики.</p>

<h2>6. Настоящая модель на GPU (отдельно от имитатора)</h2>
<p>Адаптер ДЗ 6+7 (<code>my-qwen-adapter-v2</code>, база <code>Qwen/Qwen3-0.6B</code>, bf16) на {html.escape(GPU['gpu'].split(',')[0])}.
Сервер тот же, backend <code>TransformersBackend</code>, одна генерация за раз, лимит 24 токена. Запуск на хосте, <b>не в Kubernetes</b>:
kind на Docker Desktop не пробрасывает GPU в узел. Загрузка и прогрев — {f(GPU['model_load_and_warmup_s'], 1)} с.
Части ответа — фактические токены модели, поэтому здесь используются TTFT и TPOT.</p>
<p>TTFT — от отправки до первой непустой части. TPOT = (t<sub>последней части</sub> − t<sub>первой</sub>) / (N − 1), где N — фактическое число
сгенерированных токенов (счётчик на каждом <code>put</code> streamer). Ответ из одного токена сохраняется с TPOT = null.</p>
{table(['Параллельность', 'Успешно', 'Токенов', 'Пакет, с', 'Токенов/с', 'TTFT ср., с', 'TTFT макс., с', 'TPOT ср., с', 'Ожидание в очереди ср., с'],
       [[c, g['ok'], g['output_tokens'], f(g['batch_seconds']), f(g['tokens_per_second'], 1), f(g['ttft_mean_s'], 3),
         f(g['ttft_max_s'], 3), f(g['tpot_mean_s'], 3), f(g['queue_mean_s'], 3)] for c, g in (('1', g1), ('2', g2))])}
<p>При параллельности 2 TPOT почти не меняется, а TTFT растёт за счёт ожидания: генерация последовательная, второй запрос ждёт первого.
Пропускная способность не растёт. Выборка — 8 + 8 запросов, ответы от 4 до 24 токенов (24 — упор в лимит); вывод об устойчивых перцентилях не делается.
Сырые строки: <code>results/real_model/requests.jsonl</code>.</p>

<h2>7. Собственные проверки</h2>
{table(['Проверка', 'Где', 'Что подтверждает'], [
    ['test_k8s · test_1_new_pod_gets_no_traffic_before_warmup', 'Kubernetes', 'новый Pod не получает трафик до конца прогрева; Ready ≥ 19 с после старта'],
    ['test_k8s · test_2_version_after_rollback_is_confirmed_by_answers', 'Kubernetes', 'после rollback ответы и /info всех Pod — версия A и её SHA-256'],
    ['test_k8s · test_3_replaced_pod_restores_version_and_continues_dialog', 'Kubernetes', 'замена Pod: та же версия и хеш, диалог продолжается на другом Pod'],
    ['test_k8s · test_4_two_dialogs_stay_isolated', 'Kubernetes', 'два параллельных диалога видят только свою контрольную строку'],
    ['test_local · test_each_outcome_is_counted_separately', 'локально', 'ok, 429, 5xx, оборванный поток и нет соединения считаются раздельно'],
    ['test_local · test_retries_are_separate_attempts', 'локально', 'повтор — отдельная попытка с тем же logical_id; 429 не повторяется']])}
<p>Исходный сбой: проверка 1 на исходных probes — FAIL ({red_count} запросов обслужены непрогретым Pod).
После исправления все 6 проверок проходят. Логи: <code>results/checks/red_defect_probes.log</code> и <code>results/checks/green_all.log</code>.</p>

<h2>8. Графики</h2>
<p><b>1. Прогрев и rollout.</b> Ready endpoints по времени и TTFC каждого запроса с отметкой, прогрет ли обслуживший Pod. Нагрузка 4 и 3 запроса/с.</p>
<img src="results/figures/1_warmup_rollout.png" alt="Прогрев и rollout">
<p><b>2. Задержка и очередь по Pod.</b> 80% трафика через одного клиента, прогон 1 каждой политики, 3,5 запроса/с.</p>
<img src="results/figures/2_latency_queue_by_pod.png" alt="Задержка и очередь по Pod">
<p><b>3. Сравнение политик по трём прогонам.</b> Замер 120 с, 3,5 запроса/с.</p>
<img src="results/figures/3_policy_comparison.png" alt="Сравнение политик">
<p><b>4. Масштабирование 2 → 3 → 2 вместе с нагрузкой.</b> CPU по данным HPA, нагрузка — отправленные запросы за 10 с.</p>
<img src="results/figures/4_hpa_scaling.png" alt="HPA">

<h2>9. Ревью PR: «навсегда закрепить диалог за Pod»</h2>
<p><b>Решение при этих условиях — направить запрос на B.</b> На A есть префикс, но ждать придётся около 3 с; B свободна и пересчитает префикс за 0,8 с.
Ожидаемое время до первой части на B примерно в 3–4 раза меньше. Постоянное закрепление этот выбор запрещает.
Наш опыт B показывает то же: привязка выигрывала только при случайно ровной раскладке клиентов (p95 1,4 с),
а при перекосе давала 10–12,6 с и отказы, пока соседняя реплика простаивала. Привязка должна быть предпочтением, а не правилом.
Выбирать реплику с минимальной оценкой <i>ожидание + prefill с учётом кеша</i>, а к реплике с префиксом идти, только когда разница меньше стоимости пересчёта.
Это правило проверено в опыте B как третья политика: p95 TTFC 0,73–0,77 с на горячем трафике и 1,2–1,6 с на равномерном,
без отказов — лучше и привязки, и обычного Service на тех же запросах.</p>
<p><b>Недостающие измерения.</b> Фактическое ожидание в очереди A как распределение, а не длина очереди. Зависимость пересчёта на B от длины префикса и батча:
0,8 с — для какой длины? Вероятность, что префикс на A ещё не вытеснен. Запас памяти KV на B. Доля диалогов, где такой выбор вообще возникает.
SLO по TTFC, с которым сравнивать.</p>
<p><b>Допуск нового запроса.</b> Принять на реплику, если её оценка TTFC укладывается в SLO и очередь ниже лимита. Иначе — на другую реплику,
иначе 429 с <code>Retry-After</code>, а не бесконечная очередь. Долю одной реплики ограничить сверху, например до 1,25 от средней
(ограниченная нагрузка поверх хеширования по сессии), чтобы один горячий клиент не занял всю реплику.</p>
<p><b>Потеря A.</b> История диалога приходит в запросе, поэтому любая реплика продолжит диалог, один раз пересчитав префикс.
Это проверено проверкой 3: Pod удалён, следующий ход обслужен другим Pod с правильным контекстом.
Терять из-за смерти Pod нечего, кроме кеша; закрепление «навсегда» только добавляет точку отказа.</p>
<p><b>Начатый потоковый ответ.</b> Его не переносят: он дорабатывает на своей реплике. При обновлении это обеспечивают preStop/drain и grace period:
в опыте A старый Pod доработал 27 запросов, включая потоки по 30–50 с. Если реплика умерла посреди ответа, клиент получает обрыв.
Повтор — новая попытка с полной историей, а не продолжение с середины; уже отданные части клиент помечает как прерванные.
Так это учтено и в нашем журнале: оборванный поток и повтор — две разные строки.</p>
<p><b>Кейс из лекции: Google Cloud, GKE Inference Gateway для Vertex AI.</b> Цепочка Gateway → Endpoint Picker → InferencePool → vLLM.
Запрос маршрутизируется по оценке из трёх сигналов: совпадение префикса, глубина очереди и заполнение KV-кеша; метрики берутся с Prometheus каждого Pod.
Авторы прямо пишут, что маршрутизация только по кешу опасна: популярный документ перегружает один узел, пока остальные простаивают.
Веса prefix : queue : kv-utilization изменили с 3:3:2 на 3:5:2, чтобы планировщик обходил горячие узлы даже при попадании в кеш.
Перегрузку ограничивает admission control на входе.
Результат: попадания в префиксный кеш выросли с 35% до 70%, p95 TTFT на DeepSeek V3.1 при всплесках чата снизился на 52%,
TTFT на Qwen3-Coder — более чем на 35%.
Это то же правило, что предложено выше: кеш учитывается, но очередь весит больше, а допуск ограничивает очередь.
«Навсегда закрепить» означало бы вес очереди, равный нулю.
Первичный источник: F. Feyisetan, Y. Yuan, <i>How we cut Vertex AI latency by 35% with GKE Inference Gateway</i>, Google Cloud Blog, 7 февраля 2026,
<a href="https://cloud.google.com/blog/products/containers-kubernetes/how-gke-inference-gateway-improved-latency-for-vertex-ai">cloud.google.com/blog/…/how-gke-inference-gateway-improved-latency-for-vertex-ai</a>.</p>
<p>Второй кейс лекции показывает цену жёсткой привязки при ошибке. В постмортеме Anthropic часть запросов Sonnet 4 уходила на серверы
с другой конфигурацией контекста. После изменения балансировки 29 августа в худший час затронуто 16% запросов. Маршрутизация была липкой:
следующие сообщения диалога, однажды попавшего не туда, шли на тот же неверный сервер. Привязка повторяет ошибку выбора пула,
поэтому выбор реплики нужно проверять, а не закреплять навсегда. Источник: Anthropic, <i>A postmortem of three recent issues</i>, 17 сентября 2025,
<a href="https://www.anthropic.com/engineering/a-postmortem-of-three-recent-issues">anthropic.com/engineering/a-postmortem-of-three-recent-issues</a>.</p>

<h2>10. Ограничения</h2>
<ul>
<li>Все числа опытов A–C получены на имитаторе: константы кеша и стоимости заданы конфигурацией. Это модель нагрузки, а не скорость модели.</li>
<li>Кластер из одного узла: сеть Pod→Pod локальная, сетевые задержки и межузловое размещение не проверялись.</li>
<li>Python GIL: два одновременных вычисления в одном процессе делят одно ядро. Длительность prefill задана временем, поэтому в задержки это не попадает,
но CPU одного Pod ограничен примерно одним ядром.</li>
<li>Опыт C — один прогон: время масштабирования зависит от фаз опроса metrics-server и HPA (по 15 с).</li>
<li>При первом включении HPA Helm убрал поле <code>replicas</code>, и на несколько секунд осталась одна реплика, пока HPA не вернул минимум 2.
На рабочем кластере HPA лучше включать отдельным шагом.</li>
<li>Модель на GPU измерена вне Kubernetes, на 8 + 8 запросах.</li>
</ul>
"""

page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ДЗ 11 отчёт</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"""
(ROOT / 'report.html').write_text(page, encoding='utf-8')
print('report.html', len(page))
