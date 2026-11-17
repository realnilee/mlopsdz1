"""Собирает monitoring/grafana-dashboard.json: одна функция на панель, PromQL виден целиком."""
import json
from pathlib import Path

PANELS = [
    ('TTFC p50/p95 по Pod, с', 's', [
        ('histogram_quantile(0.95, sum by (le, pod) (rate(sim_ttfc_seconds_bucket[1m])))', 'p95 {{pod}}'),
        ('histogram_quantile(0.5, sum by (le, pod) (rate(sim_ttfc_seconds_bucket[1m])))', 'p50 {{pod}}'),
        ('histogram_quantile(0.95, sum by (le) (rate(sim_ttfc_seconds_bucket[1m])))', 'p95 все')]),
    ('Полный ответ p50/p95, с', 's', [
        ('histogram_quantile(0.95, sum by (le) (rate(sim_request_duration_seconds_bucket[1m])))', 'p95'),
        ('histogram_quantile(0.5, sum by (le) (rate(sim_request_duration_seconds_bucket[1m])))', 'p50')]),
    ('running / waiting по Pod', 'short', [
        ('sum by (pod) (sim_running)', 'running {{pod}}'),
        ('sum by (pod) (sim_waiting)', 'waiting {{pod}}')]),
    ('Завершённые запросы, 1/с', 'reqps', [
        ('sum by (pod) (rate(sim_requests_total{outcome="ok"}[1m]))', '{{pod}}')]),
    ('Ошибки и отказы, 1/с', 'reqps', [
        ('sum by (outcome) (rate(sim_requests_total{outcome!="ok"}[1m]))', '{{outcome}}')]),
    ('CPU процесса, ядер', 'short', [
        ('sum by (pod) (rate(process_cpu_seconds_total{job="pods"}[1m]))', '{{pod}}')]),
    ('RAM процесса', 'bytes', [
        ('sum by (pod) (process_resident_memory_bytes{job="pods"})', '{{pod}}')]),
    ('Ready-реплики', 'short', [
        ('kube_deployment_status_replicas_ready{deployment="sim"}', 'ready'),
        ('kube_deployment_spec_replicas{deployment="sim"}', 'desired'),
        ('kube_horizontalpodautoscaler_status_current_replicas', 'HPA current')]),
]


def panel(index, title, unit, targets):
    return {'id': index + 1, 'type': 'timeseries', 'title': title,
            'datasource': {'type': 'prometheus', 'uid': 'prom'},
            'gridPos': {'h': 8, 'w': 12, 'x': 12 * (index % 2), 'y': 8 * (index // 2)},
            'fieldConfig': {'defaults': {'unit': unit}, 'overrides': []},
            'targets': [{'refId': chr(65 + i), 'expr': expr, 'legendFormat': legend}
                        for i, (expr, legend) in enumerate(targets)]}


dashboard = {'uid': 'hw11-sim', 'title': 'HW11 sim-api', 'schemaVersion': 39, 'version': 1,
             'refresh': '5s', 'time': {'from': 'now-30m', 'to': 'now'},
             'panels': [panel(i, *p) for i, p in enumerate(PANELS)]}
Path('monitoring/grafana-dashboard.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding='utf-8')
