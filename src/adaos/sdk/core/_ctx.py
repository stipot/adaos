"""Thin helpers for interacting with the process-wide :class:`AgentContext`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .errors import SdkRuntimeNotInitialized

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from adaos.services.agent_context import AgentContext


def require_ctx(feature: Optional[str] = None) -> "AgentContext":
    """Return the active :class:`AgentContext` or raise a SDK-friendly error."""

    from adaos.services.agent_context import get_ctx  # local import to avoid eager context use

    try:
        return get_ctx()
    except RuntimeError as exc:  # context not initialised yet
        raise SdkRuntimeNotInitialized(feature, str(exc)) from exc


__all__ = ["require_ctx"]
