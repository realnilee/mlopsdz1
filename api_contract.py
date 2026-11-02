"""HTTP-контракт и StubBackend, общие для прототипа и FastAPI. Только стандартная библиотека."""
import threading
import time

MAX_PROMPT_CHARS = 8000


def generation_limit(manifest):
    """Лимит берётся из протокола оценки: больший ответ не наследует прежний допуск."""
    return manifest['runtime']['generation']['max_new_tokens']


def validate_generate(payload, limit):
    """Возвращает (prompt, max_new_tokens) или ValueError — до любого обращения к backend."""
    if not isinstance(payload, dict):
        raise ValueError('тело запроса должно быть JSON-объектом')
    prompt = payload.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt должен быть непустой строкой')
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f'prompt длиннее {MAX_PROMPT_CHARS} символов')
    tokens = payload.get('max_new_tokens', limit)
    if type(tokens) is not int or not 1 <= tokens <= limit:
        raise ValueError(f'max_new_tokens должен быть целым от 1 до {limit}')
    return prompt, tokens


class StubBackend:
    """Заменяет модель в тестах управления. Ответ STUB ONLY не является ответом модели."""

    def __init__(self, manifest, root=None):
        self.loaded_id = manifest['model_id']
        self.runtime_sha256 = manifest['runtime_sha256']
        self.calls = 0
        self._lock = threading.Lock()

    def warm_up(self):
        pass

    def generate(self, prompt, limit):
        with self._lock:
            self.calls += 1
        time.sleep(.01)
        return {'answer': f'STUB ONLY: {self.loaded_id}', 'output_tokens': 1}
