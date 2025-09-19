"""Helpers for idempotent SDK operations."""

from __future__ import annotations

from typing import Optional

from ._ctx import require_ctx
from .context import get_current_skill

__all__ = ["load_request", "save_request"]


def _request_key(namespace: str, request_id: str) -> str:
    skill = get_current_skill()
    skill_id = skill.name if skill else "global"
    return f"requests/{namespace}/{skill_id}/{request_id}"


def load_request(namespace: str, request_id: str) -> Optional[dict]:
    ctx = require_ctx("Idempotency helpers require runtime context")
    key = _request_key(namespace, request_id)
    value = ctx.kv.get(key)
    return value if isinstance(value, dict) else None


def save_request(namespace: str, request_id: str, result: dict) -> None:
    ctx = require_ctx("Idempotency helpers require runtime context")
    key = _request_key(namespace, request_id)
    ctx.kv.set(key, result)
