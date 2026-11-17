"""Backend с общим интерфейсом ДЗ 8+9+10: warm_up, prefill, decode, version, sha256.

SimulatedBackend — управляемый имитатор на CPU (backend_mode=simulated): его числа
описывают модель нагрузки, а не скорость языковой модели. TransformersBackend —
настоящая модель из ДЗ 6+7 для короткого опыта на GPU вне Kubernetes.
"""
import asyncio
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path


def file_sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def spin(seconds):
    """Вычислительная работа заданной длительности: нагрузка видна в CPU-метриках."""
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        for _ in range(2000):
            x += 1
    return x


class PrefixCache:
    """Имитация локального префиксного кеша одного Pod: LRU ограниченного размера."""

    def __init__(self, size):
        self.size = size
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def touch(self, key):
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return True
            self.entries[key] = True
            while len(self.entries) > self.size:
                self.entries.popitem(last=False)
            return False

    def clear(self):
        with self.lock:
            self.entries.clear()


class SimulatedBackend:
    mode = 'simulated'

    def __init__(self, cfg):
        self.cfg = cfg
        path = Path(cfg['model_dir']) / f"model-{cfg['model_version']}.json"
        self.artifact = str(path)
        self.sha256 = file_sha256(path) if path.is_file() else None
        self.version = cfg['model_version']
        self.error = None
        if self.sha256 is None:
            self.error = f'нет файла модели {path}'
        elif cfg['model_sha256'] and self.sha256 != cfg['model_sha256']:
            self.error = f'хеш модели {self.sha256[:12]} не совпал с ожидаемым {cfg["model_sha256"][:12]}'
        self.cache = PrefixCache(cfg['cache_size'])

    def warm_up(self):
        if self.error:
            raise ValueError(self.error)
        time.sleep(self.cfg['warmup'])

    def prefill(self, key, warm):
        """Синхронно, в отдельном потоке. Возвращает hit/miss."""
        hit = self.cache.touch(key)
        spin(self.cfg['hit_cost'] if hit else self.cfg['miss_cost'])
        if not warm:
            time.sleep(self.cfg['cold_penalty'])
        return 'hit' if hit else 'miss'

    async def decode(self, n_chunks, prompt):
        for i in range(n_chunks):
            await asyncio.sleep(self.cfg['chunk_delay'])
            yield f'tok{i} ', 1


class TransformersBackend:
    """Адаптер ДЗ 6+7 на GPU. Части ответа — фактические токены модели."""
    mode = 'transformers'

    def __init__(self, cfg):
        self.cfg = cfg
        self.adapter = Path(cfg['adapter_dir'])
        manifest = json.loads((self.adapter / 'manifest.json').read_text(encoding='utf-8'))
        self.model_cfg = manifest['model']
        self.system = manifest['generation']['system']
        self.version = cfg['model_version']
        self.sha256 = file_sha256(self.adapter / 'adapter_model.safetensors')
        self.artifact = str(self.adapter)
        self.error = None
        if cfg['model_sha256'] and self.sha256 != cfg['model_sha256']:
            self.error = 'хеш адаптера не совпал'
        self.cache = PrefixCache(1)
        self.lock = threading.Lock()

    def warm_up(self):
        if self.error:
            raise ValueError(self.error)
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.device = 'cuda'
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA недоступна, а опыт с моделью должен идти на GPU')
        self.tokenizer = AutoTokenizer.from_pretrained(self.adapter, local_files_only=True)
        base = AutoModelForCausalLM.from_pretrained(self.model_cfg['name'], revision=self.model_cfg['revision'],
                                                    dtype=torch.bfloat16)
        self.model = PeftModel.from_pretrained(base, self.adapter).to(self.device).eval()
        self.gpu_name = torch.cuda.get_device_name(0)
        list(self._generate_sync('прогрев', 4))

    def prefill(self, key, warm):
        return 'n/a'

    def _generate_sync(self, prompt, limit):
        from transformers import TextIteratorStreamer
        text = self.tokenizer.apply_chat_template(
            [{'role': 'system', 'content': self.system}, {'role': 'user', 'content': prompt}],
            tokenize=False, add_generation_prompt=True, enable_thinking=False)
        batch = self.tokenizer(text, return_tensors='pt').to(self.device)
        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True)
        counter = TokenCounter()

        def run():
            with self.lock, self.torch.inference_mode():
                self.model.generate(**batch, max_new_tokens=limit, do_sample=False, streamer=_Tee(streamer, counter),
                                    pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        seen = 0
        for piece in streamer:
            tokens = counter.count - seen
            seen = counter.count
            yield piece, tokens
        thread.join()

    async def decode(self, n_chunks, prompt):
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def produce():
            try:
                for item in self._generate_sync(prompt, n_chunks):
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)
        threading.Thread(target=produce, daemon=True).start()
        while (item := await queue.get()) is not None:
            yield item


class TokenCounter:
    def __init__(self):
        self.count = 0


class _Tee:
    """Считает фактические сгенерированные токены, пропуская промпт (первый put)."""

    def __init__(self, streamer, counter):
        self.streamer, self.counter, self.first = streamer, counter, True

    def put(self, value):
        if self.first:
            self.first = False
        else:
            self.counter.count += int(value.numel())
        self.streamer.put(value)

    def end(self):
        self.streamer.end()
