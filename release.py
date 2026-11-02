"""Ядро выпуска: ключ кеша, проверка отчёта, атомарная публикация и журнал событий."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

SHA256 = re.compile(r'[0-9a-f]{64}')
HASH_FIELDS = ('model_sha256', 'protocol_sha256', 'golden_sha256', 'runtime_sha256')


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path) -> str:
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def cache_key(data_path: Path, config: dict) -> str:
    return digest(canonical({'data_sha256': file_digest(data_path), 'config': config}))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _is_text(value):
    return isinstance(value, str) and bool(value)


def _is_sha256(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _is_unit_metric(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_release(manifest: dict, report: dict) -> None:
    _require(isinstance(manifest, dict) and isinstance(report, dict), 'manifest и report должны быть объектами')
    version = manifest.get('schema_version')
    _require(type(version) is int and version == 1, 'manifest: schema_version должен быть int 1')
    for key in ('model_id', 'artifact_uri'):
        _require(_is_text(manifest.get(key)), f'manifest: пустое поле {key}')
    for key in HASH_FIELDS:
        _require(_is_sha256(manifest.get(key)), f'manifest: {key} не SHA-256')
    runtime = manifest.get('runtime')
    _require(isinstance(runtime, dict) and runtime, 'manifest: пустой runtime')
    try:
        recomputed = digest(canonical(runtime))
    except (TypeError, ValueError) as error:
        raise ValueError(f'manifest: runtime не сериализуется: {error}') from error
    _require(recomputed == manifest['runtime_sha256'], 'manifest: runtime_sha256 не соответствует runtime')

    _require(report.get('status') == 'complete', 'report: статус не complete')
    _require(report.get('passed') is True, 'report: кандидат не прошёл допуск')
    _require(_is_text(report.get('evaluation_run_id')), 'report: нет evaluation_run_id')
    for key in ('model_id', *HASH_FIELDS):
        _require(report.get(key) == manifest[key], f'report: {key} не совпадает с manifest')
    metrics = report.get('metrics')
    _require(isinstance(metrics, dict), 'report: нет metrics')
    for key in ('accuracy', 'format_rate'):
        _require(_is_unit_metric(metrics.get(key)), f'report: некорректная метрика {key}')


def atomic_write(target: Path, data: bytes) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=target.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def publish(manifest: dict, report: dict, target: Path) -> None:
    validate_release(manifest, report)
    atomic_write(target, json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))


def release_id(manifest: dict, report: dict) -> str:
    """Выпуск — это модель, runtime и решение допуска, а не только model_id."""
    return digest(canonical({'model_id': manifest['model_id'], 'model_sha256': manifest['model_sha256'],
                             'runtime_sha256': manifest['runtime_sha256'], 'protocol_sha256': manifest['protocol_sha256'],
                             'golden_sha256': manifest['golden_sha256'], 'evaluation_run_id': report['evaluation_run_id']}))


def read_journal(journal: Path) -> list:
    journal = Path(journal)
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding='utf-8').split('\n') if line.strip()]


def record_publication(manifest: dict, report: dict, journal: Path, **details) -> bool:
    """Событие пишется, только если выпуск ещё не текущий: повтор не дублирует запись,
    а сбой между указателем и журналом лечится следующим запуском. Откат A→B→A остаётся
    отдельным событием, потому что последней записью журнала к этому моменту был B."""
    rows = read_journal(journal)
    rid = release_id(manifest, report)
    if rows and rows[-1]['release_id'] == rid:
        return False
    rows.append({'release_id': rid, 'model_id': manifest['model_id'],
                 'evaluation_run_id': report['evaluation_run_id'], **details})
    atomic_write(journal, ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows).encode('utf-8'))
    return True


def append_attempt(journal: Path, **fields) -> None:
    """Журнал попыток: каждая строка — запуск, а не выпуск; дубликаты здесь допустимы."""
    with Path(journal).open('a', encoding='utf-8') as f:
        f.write(json.dumps(fields, ensure_ascii=False) + '\n')


def verify_artifact(manifest: dict, root: Path) -> None:
    """Пересчёт реальных файлов: validate_release сверяет только метаданные."""
    files = manifest.get('file_hashes')
    _require(isinstance(files, dict) and files, 'manifest: нет закреплённого списка файлов')
    root = Path(root).resolve()
    for relative, expected in files.items():
        path = (root / relative).resolve()
        _require(path.is_relative_to(root), f'путь вне артефакта: {relative}')
        _require(path.is_file(), f'нет файла артефакта: {relative}')
        _require(file_digest(path) == expected, f'хеш файла не совпал: {relative}')
    _require(digest(canonical(files)) == manifest.get('model_sha256'), 'model_sha256 не соответствует списку файлов')
