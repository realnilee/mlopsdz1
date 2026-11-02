"""Исправленный последовательный прототип. Только синтетические данные, без модели.

Целевая версия этого процесса — flows/release_flow.py.
"""
import argparse
import json
from pathlib import Path

import release
from release import canonical, digest


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def initialize(root):
    if root.exists():
        raise FileExistsError('Рабочий каталог уже существует. Выберите новый --workspace.')
    root.mkdir(parents=True)
    runtime = {'base_revision': 'fixture-only', 'tokenizer_sha256': 'a' * 64, 'template_sha256': 'b' * 64,
               'generation': {'max_new_tokens': 32, 'do_sample': False}, 'dtype': 'float32'}
    for version in ['A', 'B']:
        artifact = root / f'model-{version}.txt'
        artifact.write_text(f'STUB ONLY — version {version}\n', encoding='utf-8')
        files = {artifact.name: digest(artifact.read_bytes())}
        m = {'schema_version': 1, 'model_id': f'fixture-{version}', 'artifact_uri': artifact.name,
             'model_sha256': digest(canonical(files)), 'protocol_sha256': 'd' * 64, 'golden_sha256': 'e' * 64,
             'runtime_sha256': digest(canonical(runtime)), 'runtime': runtime, 'file_hashes': files}
        report = {k: m[k] for k in ['model_id', 'model_sha256', 'protocol_sha256', 'golden_sha256', 'runtime_sha256']}
        report.update(status='complete', passed=True, evaluation_run_id=f'fixture-eval-{version}',
                      metrics={'accuracy': .8, 'format_rate': 1.0}, synthetic=True)
        write(root / f'manifest-{version}.json', m)
        write(root / f'report-{version}.json', report)
    write(root / 'accepted.json', read(root / 'manifest-A.json'))
    report = read(root / 'report-B.json')
    report['status'] = 'running'
    report['passed'] = 'false'
    write(root / 'report-incomplete.json', report)
    report = read(root / 'report-B.json')
    report['passed'] = False
    write(root / 'report-reject.json', report)
    write(root / 'config.json', {'tokenizer_revision': 'r1', 'template_sha256': 'b' * 64, 'max_length': 128,
                                 'code_revision': 'prepare-v1'})
    (root / 'input.txt').write_text('Первый пример\nВторой пример\n', encoding='utf-8')
    print('Создан синтетический стенд:', root)


def prepare(root):
    config = read(root / 'config.json')
    key = release.cache_key(root / 'input.txt', config)
    entry = root / 'cache' / (key + '.json')
    if entry.exists():
        result = read(entry)
        print('CACHE_HIT', result['input_sha256'])
        return result
    payload = (root / 'input.txt').read_bytes()
    result = {'input_sha256': digest(payload), 'rows': len(payload.split(b'\n')) - 1, 'config': config}
    release.atomic_write(entry, json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
    print('PREPARED', result['input_sha256'])
    return result


def run(root, report_name, candidate='B', fail_after_prepare=False, fail_after_pointer=False):
    attempts = root / 'attempts.jsonl'
    try:
        prepared = prepare(root)
        if fail_after_prepare:
            raise RuntimeError('Инъекция сбоя после подготовки')
        manifest = read(root / f'manifest-{candidate}.json')
        report = read(root / report_name)
        release.verify_artifact(manifest, root)
        release.publish(manifest, report, root / 'accepted.json')
        if fail_after_pointer:
            raise RuntimeError('Инъекция сбоя между указателем и журналом')
        created = release.record_publication(manifest, report, root / 'publications.jsonl',
                                             prepared=prepared['input_sha256'])
    except Exception as error:
        release.append_attempt(attempts, candidate=candidate, report=report_name, outcome='failed', error=str(error))
        raise
    release.append_attempt(attempts, candidate=candidate, report=report_name,
                           outcome='published' if created else 'already_published')
    print('PUBLISHED' if created else 'ALREADY_PUBLISHED', manifest['model_id'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    run_parser = sub.add_parser('run')
    run_parser.add_argument('--candidate', default='B', choices=['A', 'B'])
    run_parser.add_argument('--report')
    run_parser.add_argument('--fail-after-prepare', action='store_true')
    run_parser.add_argument('--fail-after-pointer', action='store_true')
    args = parser.parse_args()
    if args.command == 'init':
        initialize(args.workspace)
    else:
        run(args.workspace, args.report or f'report-{args.candidate}.json', args.candidate,
            args.fail_after_prepare, args.fail_after_pointer)


if __name__ == '__main__':
    main()
