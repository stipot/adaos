"""Runtime helpers for accessing the AgentContext lazily."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adaos.services.agent_context import get_ctx

from .errors import SdkRuntimeNotInitialized

if TYPE_CHECKING:  # pragma: no cover
    from adaos.services.agent_context import AgentContext


def require_ctx(message: str) -> "AgentContext":
    """Return the active :class:`AgentContext` or raise a friendly SDK error."""

    try:
        return get_ctx()
    except RuntimeError as exc:  # context not initialised yet
        raise SdkRuntimeNotInitialized(message, detail=str(exc)) from exc
