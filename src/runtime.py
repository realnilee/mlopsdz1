"""Устройство, тип весов, сид и память — всё, что зависит от железа.

Код обучения не знает, на чём он запущен: устройство выбирается здесь по
params.yaml. Смена железа — правка конфига, а не кода.
"""

import os
import random

import numpy as np
import psutil
import torch

DTYPES = {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}


def resolve_device(name: str) -> torch.device:
    """auto -> cuda, если есть; иначе mps; иначе cpu."""
    if name != "auto":
        return torch.device(name)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def resolve_dtype(name: str) -> torch.dtype:
    if name not in DTYPES:
        raise SystemExit(f"model.dtype = {name!r}, допустимо: {sorted(DTYPES)}")
    return DTYPES[name]


def set_seed(seed: int) -> None:
    """Один сид на всё, что тянет случайность: инициализацию A в LoRA,
    dropout, порядок примеров. Без него два прогона с одним конфигом —
    два разных эксперимента, и сравнивать их нельзя.

    На CUDA одного manual_seed недостаточно: часть ядер cuBLAS/атомарных
    редукций в backward выбирает алгоритм недетерминированно, и кривые
    расходятся не сразу, а через несколько шагов (первые шаги совпадают
    случайно, пока расхождение не накопится). CUBLAS_WORKSPACE_CONFIG
    обязан быть выставлен ДО первого обращения к cuBLAS — здесь он
    выставляется раньше, чем main() трогает GPU (до load_model/.to(device)).
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.use_deterministic_algorithms(True, warn_only=True)
    if torch.cuda.is_available():
        # scaled_dot_product_attention по умолчанию выбирает memory-efficient
        # backend для backward — он быстрый, но недетерминированный (сам
        # PyTorch предупреждает об этом). use_deterministic_algorithms его не
        # запрещает, только предупреждает (warn_only=True). Математический
        # backend — единственный детерминированный; для модели такого
        # размера разница в скорости не важна, а воспроизводимость обязательна.
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_math_sdp(True)


def allocated_bytes(device: torch.device) -> int:
    """Память, занятая тензорами сейчас. На ускорителе — его аллокатор, а не RSS:
    урок ДЗ 2, на Apple Silicon RSS не видит буферы Metal."""
    if device.type == "mps":
        return torch.mps.driver_allocated_memory()
    if device.type == "cuda":
        return torch.cuda.memory_allocated()
    return psutil.Process().memory_info().rss


def memory_metric(device: torch.device) -> str:
    return {
        "mps": "torch.mps.driver_allocated_memory",
        "cuda": "torch.cuda.memory_allocated",
    }.get(device.type, "RSS процесса")
