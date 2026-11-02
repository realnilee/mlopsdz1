"""Синтетическая пара manifest/report для тестов записи. Качества модели не доказывает."""
import json
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent / 'examples'


def pair():
    load = lambda name: json.loads((EXAMPLES / name).read_text(encoding='utf-8'))
    return load('manifest.example.json'), load('report.example.json')
