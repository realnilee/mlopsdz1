"""Настоящий HTTP и настоящие процессы; вместо модели StubBackend."""
import time
import unittest

from tests_integration.harness import Workspace, http

VALID = {'prompt': 'Вопрос', 'max_new_tokens': 8}


class ServiceTests(Workspace):
    def ready(self, base):
        return http('GET', base + '/ready')[0] == 200

    def test_ready_and_generate_blocked_during_warmup(self):
        process, base = self.start_service(warmup=6)
        self.assertEqual(http('GET', base + '/health')[0], 200)
        self.assertEqual(http('GET', base + '/ready')[0], 503)
        self.assertEqual(http('POST', base + '/generate', VALID)[0], 503)
        self.assertEqual(http('GET', base + '/debug')[1]['calls'], 0)
        self.wait_for(lambda: self.ready(base), process)
        self.assertEqual(http('POST', base + '/generate', VALID)[0], 200)

    def test_bad_requests_get_4xx_without_backend_call(self):
        process, base = self.start_service()
        self.wait_for(lambda: self.ready(base), process)
        before = http('GET', base + '/debug')[1]['calls']
        bad = [{'prompt': '', 'max_new_tokens': 8}, {'prompt': '   ', 'max_new_tokens': 8},
               {'prompt': 'x', 'max_new_tokens': 0}, {'prompt': 'x', 'max_new_tokens': 100000},
               {'prompt': 'x', 'max_new_tokens': 33}, {'prompt': 'x', 'max_new_tokens': '8'},
               {'prompt': 'x', 'max_new_tokens': True}, {'prompt': 7}, {'max_new_tokens': 8}, ['x'],
               {'prompt': 'x' * 20000}]
        for payload in bad:
            with self.subTest(payload=str(payload)[:60]):
                status, _ = http('POST', base + '/generate', payload)
                self.assertTrue(400 <= status < 500, status)
                self.assertEqual(http('GET', base + '/debug')[1]['calls'], before)
        status, body = http('POST', base + '/generate', {'prompt': 'x', 'max_new_tokens': 32})
        self.assertEqual(status, 200)
        self.assertEqual(http('GET', base + '/debug')[1]['calls'], before + 1)
        for field in ('answer', 'request_id', 'model_id', 'runtime_sha256', 'output_tokens', 'generation_seconds'):
            self.assertIn(field, body)

    def test_reported_version_is_the_loaded_one_through_a_b_a(self):
        process, base = self.start_service()
        self.wait_for(lambda: self.ready(base), process)
        self.publish_in_other_process('B')
        _, body = http('POST', base + '/generate', VALID)
        self.assertIn('fixture-A', body['answer'])
        self.assertEqual(body['model_id'], 'fixture-A', 'accepted.json сменился, загружена всё ещё A')
        self.stop_service(process)
        for version in ('B', 'A'):
            if version == 'A':
                self.publish_in_other_process('A')
            process, base = self.start_service()
            self.wait_for(lambda: self.ready(base), process)
            _, body = http('POST', base + '/generate', VALID)
            self.assertIn(f'fixture-{version}', body['answer'])
            self.assertEqual(body['model_id'], f'fixture-{version}')
            self.stop_service(process)

    def test_tampered_artifact_is_never_served(self):
        (self.ws / 'model-A.txt').write_text('подмена весов\n', encoding='utf-8')
        process, base = self.start_service(wait_health=False)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None:
            try:
                self.assertNotEqual(http('GET', base + '/ready')[0], 200)
                self.assertNotEqual(http('POST', base + '/generate', VALID)[0], 200)
            except OSError:
                pass
            time.sleep(.2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
