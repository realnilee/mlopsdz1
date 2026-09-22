"""Стадия split: разбиение на train/val/test."""

import json
import random
import time
from pathlib import Path

from src.config import load_params
from src.contamination import report
from src.schema import Example, dump, iter_examples
from src.textnorm import normalize_group


def group_split(
    group_of: list[str], ratios: dict[str, float], seed: int
) -> list[str]:
    """Раздать строкам метки сплита ЦЕЛЫМИ ГРУППАМИ, в заданных долях по числу строк.

    Резать по индексу строки, игнорируя группу, нельзя: одна и та же тема
    (и её парафразы) окажется одновременно в train и в test — split.group_key
    существует ровно для того, чтобы этого не происходило.

    Группы перемешиваются целиком (одна тема — одна монета), а не строки
    внутри них: иначе тема просто дробится в других пропорциях, но всё
    равно утекает между сплитами.
    """
    groups: dict[str, list[int]] = {}
    for i, key in enumerate(group_of):
        groups.setdefault(key, []).append(i)

    keys = list(groups)
    random.Random(seed).shuffle(keys)

    total = len(group_of)
    names = list(ratios)
    cum = 0.0
    boundaries = []
    for i, name in enumerate(names):
        cum += ratios[name]
        boundaries.append(total if i == len(names) - 1 else round(total * cum))

    labels = [""] * total
    bucket = 0
    filled = 0
    for key in keys:
        idxs = groups[key]
        while bucket < len(names) - 1 and filled >= boundaries[bucket]:
            bucket += 1
        for i in idxs:
            labels[i] = names[bucket]
        filled += len(idxs)
    return labels


def main() -> None:
    params = load_params()
    paths = params["paths"]
    cfg = params["split"]
    started = time.perf_counter()

    examples: list[Example] = list(iter_examples(paths["clean"]))
    if cfg["group_key"] != "topic":
        raise SystemExit(f"неизвестный split.group_key: {cfg['group_key']!r}")

    group_of = [normalize_group(ex.topic) for ex in examples]
    sizes: dict[str, int] = {}
    for key in group_of:
        sizes[key] = sizes.get(key, 0) + 1

    labels = group_split(group_of, cfg["ratios"], cfg["seed"])
    buckets: dict[str, list[Example]] = {name: [] for name in cfg["ratios"]}
    for label, ex in zip(labels, examples):
        buckets[label].append(ex)

    for name, rows in buckets.items():
        out = Path(paths[name])
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for ex in rows:
                fh.write(dump(ex) + "\n")

    nd = params["clean"]["near_dup"]
    rep = report(
        buckets["train"],
        buckets["test"],
        shingle_words=nd["shingle_words"],
        num_perm=nd["num_perm"],
        threshold=params["contamination"]["threshold"],
    )

    metrics = {
        "version": params["collect"]["version"],
        "seed": cfg["seed"],
        "group_key": cfg["group_key"],
        "groups_total": len(sizes),
        "sizes": {name: len(rows) for name, rows in buckets.items()},
        "groups": {
            name: len({normalize_group(ex.topic) for ex in rows}) for name, rows in buckets.items()
        },
        "ratios_actual": {
            name: round(len(rows) / len(examples), 4) for name, rows in buckets.items()
        },
        "contamination": rep,
        "seconds": round(time.perf_counter() - started, 2),
    }
    mpath = Path(paths["metrics_split"])
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "split: "
        + ", ".join(f"{name} {len(rows)}" for name, rows in buckets.items())
        + f" (групп {len(sizes)}, {metrics['seconds']} с)"
    )


if __name__ == "__main__":
    main()
