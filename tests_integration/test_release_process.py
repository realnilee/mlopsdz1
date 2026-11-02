"""Процесс выпуска через subprocess с отдельным рабочим каталогом на каждый тест."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release
from tests_integration.harness import Workspace, sha


class ProcessTests(Workspace):
    def edit_json(self, name, **changes):
        path = self.ws / name
        path.write_text(json.dumps({**json.loads(path.read_text(encoding='utf-8')), **changes}), encoding='utf-8')

    def test_recovery_uses_cache_and_any_input_change_invalidates(self):
        crashed = self.lab('run', '--fail-after-prepare')
        self.assertNotEqual(crashed.returncode, 0)
        self.assertIn('PREPARED', crashed.stdout)
        recovered = self.lab('run')
        self.assertEqual(recovered.returncode, 0)
        self.assertIn('CACHE_HIT', recovered.stdout)
        self.assertNotIn('PREPARED', recovered.stdout)
        (self.ws / 'input.txt').write_text('Другие байты по тому же пути\n', encoding='utf-8')
        changed = self.lab('run')
        self.assertIn('PREPARED', changed.stdout, 'изменение байтов обязано инвалидировать кеш')
        for key, value in [('tokenizer_revision', 'r2'), ('template_sha256', 'f' * 64),
                           ('max_length', 256), ('code_revision', 'prepare-v2')]:
            with self.subTest(key=key):
                self.edit_json('config.json', **{key: value})
                self.assertIn('PREPARED', self.lab('run').stdout)

    def test_incomplete_rejected_foreign_reports_keep_pointer(self):
        report = json.loads((self.ws / 'report-B.json').read_text(encoding='utf-8'))
        (self.ws / 'report-nan.json').write_text(
            json.dumps({**report, 'metrics': {'accuracy': float('nan'), 'format_rate': 1.0}}), encoding='utf-8')
        before = sha(self.ws / 'accepted.json')
        for name in ('report-incomplete.json', 'report-reject.json', 'report-A.json', 'report-nan.json'):
            with self.subTest(report=name):
                self.assertNotEqual(self.lab('run', '--report', name).returncode, 0)
                self.assertEqual(sha(self.ws / 'accepted.json'), before)
                self.assertEqual(self.events('fixture-B'), [])

    def test_repeat_publish_makes_one_event_new_evaluation_makes_another(self):
        self.assertEqual(self.lab('run').returncode, 0)
        pointer = sha(self.ws / 'accepted.json')
        self.assertEqual(self.lab('run').returncode, 0)
        self.assertEqual(self.lab('run').returncode, 0)
        self.assertEqual(sha(self.ws / 'accepted.json'), pointer)
        self.assertEqual(len(self.events('fixture-B')), 1)
        self.edit_json('report-B.json', evaluation_run_id='fixture-eval-B-2')
        self.assertEqual(self.lab('run').returncode, 0)
        self.assertEqual(len(self.events('fixture-B')), 2, 'та же модель с новым решением — другой выпуск')

    def test_crash_between_pointer_and_journal_recovers_without_double_event(self):
        journal = self.ws / 'publications.jsonl'
        journal.mkdir()
        self.assertNotEqual(self.lab('run').returncode, 0)
        self.assertEqual(json.loads((self.ws / 'accepted.json').read_text(encoding='utf-8'))['model_id'], 'fixture-B')
        journal.rmdir()
        self.assertEqual(self.lab('run').returncode, 0)
        self.assertEqual(self.lab('run').returncode, 0)
        self.assertEqual(len(self.events('fixture-B')), 1)


class WriteFailureTests(unittest.TestCase):
    def test_failed_fsync_keeps_old_bytes_and_no_temp_file(self):
        from examples_fixture import pair
        manifest, report = pair()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / 'accepted.json'
            target.write_bytes(b'{"previous": true}\n')
            with patch('os.fsync', side_effect=OSError('симуляция ошибки диска')):
                with self.assertRaises(OSError):
                    release.publish(manifest, report, target)
            self.assertEqual(target.read_bytes(), b'{"previous": true}\n')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['accepted.json'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
