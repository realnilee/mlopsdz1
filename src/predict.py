"""Нужны tracking URI, имя модели и текст. Локальные параметры обучения не читаются."""
import argparse
import json
import sys

import mlflow
import pandas as pd

# tests/check.py запускает этот файл как `python -I ...` — изолированный режим
# глушит PYTHONUTF8/PYTHONIOENCODING, и stdout/stderr падают на системную
# кодировку Windows (cp1251), которая не кодирует часть символов в вопросах
# MMLU и в предупреждениях transformers/huggingface (идут в stderr). Родитель
# (subprocess.run(..., text=True)) всегда ждёт UTF-8 от обоих потоков, так что
# перенастраиваем их явно, в коде — это работает независимо от -I.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", required=True, help="models:/my-qwen-adapter@champion")
    ap.add_argument("--tracking-uri", default="http://127.0.0.1:5006")
    ap.add_argument("--prompt", required=True)
    args = ap.parse_args()
    if not args.uri.startswith("models:/"):
        raise SystemExit("Ожидается адрес models:/ из реестра")
    mlflow.set_tracking_uri(args.tracking_uri)
    model = mlflow.pyfunc.load_model(args.uri)
    print(json.dumps(model.predict(pd.DataFrame({"prompt": [args.prompt]})), ensure_ascii=False))


if __name__ == "__main__":
    main()
