"""Чтение params.yaml — единственная точка правды о конфигурации."""

import sys
from pathlib import Path

import yaml

# На Windows stdout/stderr, запущенные не из интерактивного терминала
# (dvc repro, check.sh), кодируются кодовой страницей консоли, а не UTF-8 —
# стадии печатают кириллицу, и вывод падает с UnicodeEncodeError. config.py
# импортируют все стадии первым (тот же фикс был нужен в ДЗ2/ДЗ3/ДЗ4).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def load_params(path: str = "params.yaml") -> dict:
    """Загрузить параметры запуска."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
