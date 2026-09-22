"""Стадия collect: источник → data/raw.jsonl.

Источник — MMLU (cais/mmlu, лицензия MIT): тестовые вопросы с вариантами
ответа по 57 предметам. Зеркало parquet-файлов лежит локально в
../../datasets/mmlu/ (см. params.yaml → collect.sources) — сеть при
`dvc repro` не нужна, воспроизводимость не зависит от доступности HF Hub.

Контракт стадии, а не её внутренности, держит остальной пайплайн: на выходе
JSONL со строками {"id", "topic", "messages": [system, user, assistant]}.

Исходная строка MMLU — это table row (question / choices / answer), а не
диалог, и инструкции в источнике нет вовсе. Поэтому стадия не перекладывает
parquet в JSONL один в один (README, «Готовый датасет как источник»), а
делает три вещи, и каждая видна числом в metrics/collect.json:

  1. балансирует темы — не более collect.max_per_topic строк на тему из
     каждого файла-источника, иначе professional_law занимает почти 11%
     набора (выше diversity.max_group_share);
  2. переразмечает в chat-формат: пронумерованные варианты ответа собираются
     в один user-промпт, а в assistant пишется буква и текст ВЕРНОГО варианта
     по полю answer источника;
  3. обогащает единственную (в источнике её нет) инструкцию вариантами
     (collect.system_prompts) — вариант выбирается детерминированно по id.
"""

import hashlib
import json
import time
from pathlib import Path

import pyarrow.parquet as pq

from src.config import load_params, source_files

COLUMNS = ["question", "subject", "choices", "answer"]
LETTERS = "ABCD"


def pick_prompt(example_id: str, variants: list[str]) -> str:
    """Детерминированно выбрать вариант инструкции по id примера.

    Именно sha1, а не встроенный hash(): тот солится на каждый запуск процесса,
    и raw.jsonl переставал бы быть воспроизводимым.
    """
    digest = hashlib.sha1(example_id.encode("utf-8")).hexdigest()
    return variants[int(digest, 16) % len(variants)]


def make_id(subject: str, question: str) -> str:
    """Стабильный id по содержимому, а не по позиции в файле.

    Один и тот же вопрос обязан получать один и тот же id независимо от того,
    из какого файла-источника версии v1/v2 он читается — иначе v2 не
    расширяет v1, а тасует его заново.
    """
    digest = hashlib.sha1(f"{subject}::{question}".encode("utf-8")).hexdigest()
    return f"mmlu_{digest[:16]}"


def format_user(question: str, choices: list[str]) -> str:
    lines = [f"Question:\n{question}", "", "Choices:"]
    lines += [f"{LETTERS[i]}. {choice}" for i, choice in enumerate(choices)]
    return "\n".join(lines)


def format_assistant(choices: list[str], answer: int) -> str:
    return f"Answer: {LETTERS[answer]}. {choices[answer]}"


def main() -> None:
    params = load_params()
    cfg = params["collect"]
    paths = params["paths"]
    max_per_topic = cfg["max_per_topic"]
    variants = cfg["system_prompts"]
    if not variants:
        raise SystemExit("collect.system_prompts пуст: инструкцию брать неоткуда")
    topics = cfg["topics"]
    wanted = set(topics) if topics else None

    out = Path(paths["raw"])
    out.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    scanned = written = dropped_topic = dropped_choices = dropped_cap = 0
    prompts_used: set[str] = set()
    seen_ids: set[str] = set()
    dropped_dup_id = 0

    with out.open("w", encoding="utf-8") as fh:
        for src in source_files(params):
            if not src.exists():
                raise SystemExit(f"нет файла-источника: {src}")
            # Кэп — НА ТЕМУ, В ЭТОМ ФАЙЛЕ: у каждого файла-источника свой счётчик,
            # так v2 (test + validation) добирает по каждой теме из второго файла
            # то, чего не было в первом, а не режет объединённый пул заново.
            per_topic: dict[str, int] = {}
            for batch in pq.ParquetFile(src).iter_batches(batch_size=2000, columns=COLUMNS):
                for row in batch.to_pylist():
                    scanned += 1
                    subject = row["subject"]
                    if wanted is not None and subject not in wanted:
                        dropped_topic += 1
                        continue
                    choices = row["choices"]
                    answer = row["answer"]
                    if len(choices) != 4 or not (0 <= answer < len(choices)):
                        dropped_choices += 1
                        continue
                    if per_topic.get(subject, 0) >= max_per_topic:
                        dropped_cap += 1
                        continue

                    example_id = make_id(subject, row["question"])
                    if example_id in seen_ids:
                        # Один и тот же вопрос дословно встречается в test и
                        # validation MMLU у нескольких предметов — редко, но
                        # бывает. Дубль по содержимому не должен попасть в raw
                        # дважды ещё до стадии clean.
                        dropped_dup_id += 1
                        continue
                    seen_ids.add(example_id)

                    prompt = pick_prompt(example_id, variants)
                    prompts_used.add(prompt)
                    record = {
                        "id": example_id,
                        "topic": subject,
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": format_user(row["question"], choices)},
                            {"role": "assistant", "content": format_assistant(choices, answer)},
                        ],
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    per_topic[subject] = per_topic.get(subject, 0) + 1
                    written += 1

    metrics = {
        "version": cfg["version"],
        "files": len(source_files(params)),
        "rows_scanned": scanned,
        "rows_written": written,
        "dropped_topic_filter": dropped_topic,
        "dropped_bad_choices": dropped_choices,
        "dropped_topic_cap": dropped_cap,
        "dropped_duplicate_id": dropped_dup_id,
        "max_per_topic": max_per_topic,
        "topics_filter": len(wanted) if wanted else 0,
        "system_prompt_variants": len(prompts_used),
        "seconds": round(time.perf_counter() - started, 2),
    }
    mpath = Path(paths["metrics_collect"])
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"collect: версия {cfg['version']}, файлов {metrics['files']}, "
        f"просмотрено {scanned}, записано {written} "
        f"(кэп темы -{dropped_cap}, битые варианты -{dropped_choices}, "
        f"дубль id -{dropped_dup_id}), "
        f"вариантов инструкции {len(prompts_used)}, "
        f"{metrics['seconds']} с → {out}"
    )


if __name__ == "__main__":
    main()
