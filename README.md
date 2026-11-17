# ДЗ 11. Сервис в Kubernetes под нагрузкой

Продолжение сервиса ДЗ 8+9+10: тот же интерфейс backend (`warm_up`, генерация, `version`, хеш артефакта),
HTTP на FastAPI, потоковая выдача NDJSON. Основная часть работает на **имитаторе** (`backend_mode=simulated`,
CPU, без весов). Короткий опыт с настоящей моделью ДЗ 6+7 выполнен отдельно на GPU (раздел «Модель»).

Отчёт: [`report.html`](report.html). Сырые данные: [`results/`](results/).

## Состав

| Путь | Что |
|---|---|
| `app/server.py` | API: `/generate` (поток), `/health`, `/startup`, `/ready`, `/live`, `/info`, `/metrics`, `/drain` |
| `app/backends.py` | `SimulatedBackend` (прогрев, LRU-кеш префиксов, стоимость hit/miss) и `TransformersBackend` (GPU) |
| `app/loadgen.py` | генератор нагрузки с расписанием (открытая модель), учёт исходов и повторов |
| `Dockerfile`, `requirements.txt` | образ `sim-api:0.1.0`; точные версии внутри образа — `/srv/requirements.lock.txt` |
| `chart/sim-api/` | Helm-чарт: Deployment, ConfigMap, два Service, HPA |
| `k8s/clients.yaml` | 4 клиентских Pod для генератора |
| `monitoring/` | Prometheus (discovery по Pod), правила тревог, Grafana и экспорт дашборда, metrics-server |
| `experiments/` | опыты A, B, C |
| `tests/` | собственные проверки |
| `real_model/bench.py` | опыт с настоящей моделью на GPU |

## Окружение и лимиты

Windows 11, Docker Desktop 29.9.2 (WSL2, 16 vCPU, 15,5 ГиБ), kind v0.33.0 (узел `kindest/node:v1.37.0`),
kubectl v1.36.1, Helm v4.3.2. Свободно на диске до начала: 69 ГБ.

| Компонент | Образ | Размер | Requests / limits |
|---|---|---|---|
| sim-api ×2–3 | `sim-api:0.1.0` (python:3.12.12-slim) | 202 МБ (49 МБ сжато) | 200m / 1 CPU, 128 / 256 МиБ |
| loadgen ×4 | тот же | — | 100m / 1 CPU, 64 / 256 МиБ |
| Prometheus | `prom/prometheus:v3.1.0` | 117 МБ | хранение 6 ч, не более 800 МБ, emptyDir 1 ГиБ |
| Grafana | `grafana/grafana:11.4.0` | 133 МБ | без постоянного тома |
| kube-state-metrics | `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0` | 14 МБ | |
| metrics-server | `registry.k8s.io/metrics-server/metrics-server:v0.7.2` | 20 МБ | `--kubelet-insecure-tls` для kind |

Фактический размер стенда: узел kind — образ около 1 ГБ и том около 2,3 ГБ; образ `sim-api` 0,2 ГБ.
Окружение `.venv` для опыта с моделью на GPU (torch cu130) занимает несколько ГБ, его можно удалить отдельно.
Подробности: `results/stand/versions.txt`.

## Запуск

Из Git Bash в этой папке:

```bash
. ./env.sh                                    # пути к kubectl, kind, helm
kind create cluster --config kind.yaml
docker build -t sim-api:0.1.0 .
kind load docker-image sim-api:0.1.0 --name hw11
helm upgrade --install sim chart/sim-api --wait
kubectl apply -f k8s/clients.yaml

# мониторинг
python tools/make_dashboard.py
kubectl apply -f monitoring/stack.yaml
kubectl -n monitoring create configmap prometheus-config --from-file=monitoring/prometheus.yml --from-file=monitoring/rules.yml
kubectl -n monitoring create configmap grafana-datasources --from-file=datasources.yaml=monitoring/grafana-datasources.yaml
kubectl -n monitoring create configmap grafana-providers --from-file=providers.yaml=monitoring/grafana-providers.yaml
kubectl -n monitoring create configmap grafana-dashboards --from-file=hw11.json=monitoring/grafana-dashboard.json
kubectl apply -f monitoring/metrics-server-v0.7.2.yaml

# Grafana: http://127.0.0.1:3000, Prometheus: http://127.0.0.1:9090
kubectl -n monitoring port-forward svc/grafana 3000:3000 &
kubectl -n monitoring port-forward svc/prometheus 9090:9090 &
```

Опыты и проверки (Python 3.10+ на хосте, без зависимостей; графики — `.venv` с matplotlib):

```bash
cd experiments
python exp_a.py                 # прогрев (дефект и исправление), rollout, неготовая B и откат, kill
python exp_b.py                 # 2 политики × 2 вида трафика × 3 прогона по 120 с (~30 мин)
python exp_c.py                 # HPA 2→3→2 и тревоги
cd ..
python -m unittest tests.test_local -v
python -m unittest tests.test_k8s -v
.venv/Scripts/python.exe tools/analyze.py      # results/summary.json, results/requests.csv, графики
.venv/Scripts/python.exe real_model/bench.py   # модель ДЗ 6+7 на GPU, вне кластера
.venv/Scripts/python.exe tools/build_report.py # report.html
```

Исходный дефект probes воспроизводится так: `helm upgrade sim chart/sim-api --set probes.fixed=false --wait`.

## Удаление

```bash
kind delete cluster --name hw11        # узел, его том и все Pod
docker image rm sim-api:0.1.0 kindest/node:v1.37.0
rm -rf .venv                           # окружение для GPU-опыта
```

Секретов в стенде нет; Grafana работает без логина только на localhost через port-forward.
