from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Tuple

from adaos.services.agent_context import AgentContext

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


def _validate_key(key: str) -> str:
    if not key:
        raise ValueError("state key must be non-empty")
    if key.startswith("/"):
        raise ValueError("state key must be relative")
    if ".." in key.split("/"):
        raise ValueError("state key must not contain path traversal")
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("state key contains unsupported characters")
    return key


@dataclass(slots=True)
class SkillStateService:
    ctx: AgentContext

    def _state_prefix(self, skill_id: str | None) -> str:
        if skill_id:
            return f"skills/{skill_id}/state"
        return "global/state"

    def _request_prefix(self, skill_id: str | None) -> str:
        if skill_id:
            return f"skills/{skill_id}/requests"
        return "global/requests"

    def get(self, skill_id: str | None, key: str, default: Any = None) -> Tuple[Any, bool]:
        key = _validate_key(key)
        storage_key = f"{self._state_prefix(skill_id)}/{key}"
        sentinel = object()
        value = self.ctx.kv.get(storage_key, sentinel)
        if value is sentinel:
            return default, False
        return value, True

    def set(self, skill_id: str | None, key: str, value: Any) -> str:
        key = _validate_key(key)
        storage_key = f"{self._state_prefix(skill_id)}/{key}"
        self.ctx.kv.set(storage_key, value)
        return storage_key

    def request_key(self, skill_id: str | None, request_id: str) -> str:
        if not request_id:
            raise ValueError("request_id must be non-empty")
        if ".." in request_id.split("/"):
            raise ValueError("request_id must not contain path traversal")
        return f"{self._request_prefix(skill_id)}/{request_id}"
