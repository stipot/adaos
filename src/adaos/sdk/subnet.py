# src\adaos\sdk\subnet.py
# SDK-обёртка для навыков
from typing import Any
from adaos.agent.core.subnet_context import CTX


def subnet_get(key: str, default: Any = None) -> Any:
    return CTX.get(key, default)


def subnet_set(key: str, value: Any) -> bool:
    return CTX.set(key, value)
