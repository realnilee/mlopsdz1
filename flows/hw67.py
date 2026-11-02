"""Мост к ДЗ 6+7: подготовка входа, чтение training run и вызов настоящего quality_gate."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import lab
import release
from release import canonical, digest, file_digest

HW67 = Path(os.environ.get('HW67_ROOT', Path(__file__).resolve().parents[3] / 'дз 6-7' / 'mlops26-hw6-7' / 'hw67-broken'))
TRACKING_URI = os.environ.get('MLFLOW_TRACKING_URI', 'http://127.0.0.1:5006')
ADAPTER_FILES = ['adapter_config.json', 'adapter_model.safetensors', 'chat_template.jinja',
                 'manifest.json', 'tokenizer.json', 'tokenizer_config.json']
EVIDENCE = ['baseline-eval.json', 'candidate-eval.json', 'baseline-outputs.json', 'candidate-outputs.json']
GATE_CODE = '''
import json, sys
from evaluation import cli
from evaluation.contract import read_json
try:
    cli.gate()
    print(json.dumps({"passed": True, "gate": read_json("metrics/quality-gate.json")}))
except ValueError as error:
    print(json.dumps({"passed": False, "reason": str(error)}, ensure_ascii=False))
'''


def read_hw67(relative):
    return json.loads((HW67 / relative).read_text(encoding='utf-8'))


def initialize(root):
    if root.exists():
        raise FileExistsError('Рабочий каталог уже существует. Выберите новый --workspace.')
    root.mkdir(parents=True)
    shutil.copyfile(HW67 / 'data/eval/golden.jsonl', root / 'input.txt')
    from transformers import AutoTokenizer
    baseline = read_hw67('evaluation/baseline.json')
    tokenizer = AutoTokenizer.from_pretrained(baseline['name'], revision=baseline['revision'])
    lab.write(root / 'config.json', {'tokenizer_name': baseline['name'], 'tokenizer_revision': baseline['revision'],
                                     'template_sha256': digest(tokenizer.chat_template.encode('utf-8')),
                                     'max_length': 512, 'code_revision': 'prepare-golden-v1'})
    print('Создан реальный рабочий каталог:', root)


def prepare_input(input_path, config, workspace):
    """На стенде считает строки; в реальном потоке проверяет схему и токенизирует golden set."""
    payload = input_path.read_bytes()
    result = {'input_sha256': digest(payload), 'rows': len(payload.split(b'\n')) - 1, 'config': config}
    if config.get('tokenizer_name'):
        from transformers import AutoTokenizer
        rows = [json.loads(line) for line in payload.decode('utf-8').split('\n') if line.strip()]
        if any(not row.get('expected') for row in rows):
            raise ValueError('В golden есть строка без expected')
        tokenizer = AutoTokenizer.from_pretrained(config['tokenizer_name'], revision=config['tokenizer_revision'])
        if digest(tokenizer.chat_template.encode('utf-8')) != config['template_sha256']:
            raise ValueError('Шаблон токенизатора не совпадает с конфигурацией')
        lengths = [len(tokenizer(row['input'])['input_ids']) for row in rows]
        result.update(rows=len(rows), max_tokens=max(lengths), over_limit=sum(n > config['max_length'] for n in lengths))
    return result


def fetch_manifest(workspace):
    """Скачивает файлы уже обученного адаптера и закрепляет их хеши в manifest."""
    import mlflow
    from mlflow import MlflowClient
    mlflow.set_tracking_uri(TRACKING_URI)
    spec = read_hw67('metrics/candidate-model.json')
    params = read_hw67('evaluation/params.json')
    baseline = read_hw67('evaluation/baseline.json')
    candidate = read_hw67('metrics/candidate-eval.json')
    target = workspace / 'artifact'
    if not (target / 'adapter' / 'adapter_config.json').exists():
        client = MlflowClient(tracking_uri=TRACKING_URI)
        download = workspace / '.download'
        download.mkdir(exist_ok=True)
        client.download_artifacts(spec['run_id'], 'model/artifacts/adapter', str(download))
        target.mkdir(exist_ok=True)
        shutil.move(str(download / 'model' / 'artifacts' / 'adapter'), str(target / 'adapter'))
        shutil.rmtree(download)
    adapter = target / 'adapter'
    files = {f'artifact/adapter/{name}': file_digest(adapter / name) for name in ADAPTER_FILES}
    runtime = {'base_model': baseline['name'], 'base_revision': baseline['revision'],
               'tokenizer_sha256': file_digest(adapter / 'tokenizer.json'),
               'template_sha256': file_digest(adapter / 'chat_template.jinja'),
               'generation': {'system': params['system'], 'do_sample': False, 'seed': params['seed'],
                              'max_new_tokens': params['max_new_tokens'], 'enable_thinking': params['enable_thinking']},
               'dtype': 'bfloat16'}
    return {'schema_version': 1, 'model_id': f"{spec['name']}-v{spec['version']}-{spec['run_id'][:8]}",
            'artifact_uri': spec['uri'], 'training_run_id': spec['run_id'], 'adapter_path': 'artifact/adapter',
            'model_sha256': digest(canonical(files)), 'protocol_sha256': candidate['protocol_sha256'],
            'golden_sha256': candidate['golden_sha256'], 'runtime_sha256': digest(canonical(runtime)),
            'runtime': runtime, 'file_hashes': files}


def build_report(manifest, workspace):
    """Решение принимает quality_gate ДЗ 6+7; обёртка только переносит его результат."""
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': str(HW67), 'MLFLOW_TRACKING_URI': TRACKING_URI}
    done = subprocess.run([sys.executable, '-c', GATE_CODE], cwd=HW67, env=env, capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=300)
    if done.returncode != 0:
        raise RuntimeError(f'Проверка допуска не выполнилась: {done.stderr[-800:]}')
    decision = json.loads(done.stdout.strip().split('\n')[-1])
    baseline, candidate = read_hw67('metrics/baseline-eval.json'), read_hw67('metrics/candidate-eval.json')
    report = {**{k: manifest[k] for k in ('model_id', 'model_sha256', 'runtime_sha256')},
              'protocol_sha256': candidate['protocol_sha256'], 'golden_sha256': candidate['golden_sha256'],
              'status': candidate['status'], 'passed': decision['passed'], 'reason': decision.get('reason'),
              'evaluation_run_id': candidate['evaluation_run_id'], 'training_run_id': manifest['training_run_id'],
              'metrics': {k: candidate['overall'][k] for k in ('accuracy', 'format_rate')},
              'baseline_metrics': {k: baseline['overall'][k] for k in ('accuracy', 'format_rate')},
              'behavioral': candidate['behavioral']['overall'], 'gate': decision.get('gate'),
              'sources': {name: {'path': f'metrics/{name}', 'sha256': file_digest(HW67 / 'metrics' / name)} for name in EVIDENCE}}
    # Отклонённый отчёт сохраняется так же, как принятый.
    release.atomic_write(workspace / 'release-report.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
    return report
