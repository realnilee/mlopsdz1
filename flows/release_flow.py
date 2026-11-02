"""Prefect-flow выпуска. Команды совместимы с lab.py; --real читает результаты ДЗ 6+7.

Между задачами передаются URI, ID и хеши, но не веса и не тензоры.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import lab
import release
from flows import hw67


def run_flow(workspace, candidate, report_name, real, fail_after_prepare, fail_after_pointer):
    # Результаты кеша лежат в рабочем каталоге и переживают завершение процесса.
    os.environ['PREFECT_LOCAL_STORAGE_PATH'] = str((workspace / '.prefect-results').resolve())
    from prefect import flow, task

    def preparation_key(_, parameters):
        return release.cache_key(Path(parameters['input_path']), parameters['config'])

    @task(name='prepare', cache_key_fn=preparation_key, persist_result=True)
    def prepare(input_path, config):
        return hw67.prepare_input(Path(input_path), config, workspace)

    @task(name='fetch-artifact', retries=2, retry_delay_seconds=2)
    def fetch_artifact():
        """Читает готовый training run: это не повторное обучение."""
        if real:
            return hw67.fetch_manifest(workspace)
        return lab.read(workspace / f'manifest-{candidate}.json')

    @task(name='verify-artifact')
    def verify_artifact(manifest):
        release.verify_artifact(manifest, workspace)

    @task(name='admission-gate')
    def admission_gate(manifest):
        if real:
            return hw67.build_report(manifest, workspace)
        return lab.read(workspace / report_name)

    @task(name='publish')
    def publish(manifest, report, input_sha256):
        release.publish(manifest, report, workspace / 'accepted.json')
        if fail_after_pointer:
            raise RuntimeError('Инъекция сбоя между указателем и журналом')
        return release.record_publication(manifest, report, workspace / 'publications.jsonl', prepared=input_sha256)

    @flow(name='release-model', log_prints=False)
    def release_model():
        config = lab.read(workspace / 'config.json')
        state = prepare(str(workspace / 'input.txt'), config, return_state=True)
        prepared = state.result()
        print('CACHE_HIT' if state.name == 'Cached' else 'PREPARED', prepared['input_sha256'], f'state={state.name}', flush=True)
        if fail_after_prepare:
            raise RuntimeError('Инъекция сбоя после подготовки')
        manifest = fetch_artifact()
        verify_artifact(manifest)
        report = admission_gate(manifest)
        created = publish(manifest, report, prepared['input_sha256'])
        print('PUBLISHED' if created else 'ALREADY_PUBLISHED', manifest['model_id'], flush=True)
        return created

    attempts = workspace / 'attempts.jsonl'
    if real:
        candidate, report_name = 'hw67-candidate', 'release-report.json'
    try:
        created = release_model()
    except Exception as error:
        release.append_attempt(attempts, candidate=candidate, report=report_name, outcome='failed', error=str(error))
        print(f'FAILED {type(error).__name__}: {error}', flush=True)
        return 1
    release.append_attempt(attempts, candidate=candidate, report=report_name,
                           outcome='published' if created else 'already_published')
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab'))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--real', action='store_true')
    run = sub.add_parser('run')
    run.add_argument('--candidate', default='B', choices=['A', 'B'])
    run.add_argument('--report')
    run.add_argument('--real', action='store_true')
    run.add_argument('--fail-after-prepare', action='store_true')
    run.add_argument('--fail-after-pointer', action='store_true')
    args = parser.parse_args()
    if args.command == 'init':
        hw67.initialize(args.workspace) if args.real else lab.initialize(args.workspace)
        return 0
    return run_flow(args.workspace, args.candidate, args.report or f'report-{args.candidate}.json', args.real,
                    args.fail_after_prepare, args.fail_after_pointer)


if __name__ == '__main__':
    sys.exit(main())
