"""Настоящий backend. Интерфейс совпадает со StubBackend: warm_up, generate, loaded_id, calls."""
from pathlib import Path

from release import file_digest


class TransformersBackend:
    def __init__(self, manifest, root):
        self.manifest = manifest
        self.loaded_id = manifest['model_id']
        self.runtime_sha256 = manifest['runtime_sha256']
        self.runtime = manifest['runtime']
        self.adapter = Path(root) / manifest['adapter_path']
        self.calls = 0
        self.device = None

    def warm_up(self):
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        runtime = self.runtime
        if file_digest(self.adapter / 'tokenizer.json') != runtime['tokenizer_sha256']:
            raise ValueError('Токенизатор не совпадает с runtime оценки')
        if file_digest(self.adapter / 'chat_template.jinja') != runtime['template_sha256']:
            raise ValueError('Шаблон не совпадает с runtime оценки')
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.tokenizer = AutoTokenizer.from_pretrained(self.adapter, local_files_only=True)
        base = AutoModelForCausalLM.from_pretrained(runtime['base_model'], revision=runtime['base_revision'],
                                                    dtype=getattr(torch, runtime['dtype']))
        self.model = PeftModel.from_pretrained(base, self.adapter).to(self.device).eval()
        self._generate('прогрев', 1)
        self.calls = 0

    def _generate(self, prompt, limit):
        import torch
        g = self.runtime['generation']
        text = self.tokenizer.apply_chat_template(
            [{'role': 'system', 'content': g['system']}, {'role': 'user', 'content': prompt}],
            tokenize=False, add_generation_prompt=True, enable_thinking=g['enable_thinking'])
        batch = self.tokenizer(text, return_tensors='pt').to(self.device)
        with torch.inference_mode():
            out = self.model.generate(**batch, max_new_tokens=limit, do_sample=g['do_sample'],
                                      pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id)
        new_tokens = out[0, batch['input_ids'].shape[1]:]
        return {'answer': self.tokenizer.decode(new_tokens, skip_special_tokens=True),
                'output_tokens': int(new_tokens.shape[0]), 'input_tokens': int(batch['input_ids'].shape[1])}

    def generate(self, prompt, limit):
        self.calls += 1
        return self._generate(prompt, limit)
