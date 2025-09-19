"""Runtime helpers shared by SDK facades."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from adaos.services.agent_context import get_ctx

from .errors import SdkRuntimeNotInitialized

if TYPE_CHECKING:  # pragma: no cover - import for type checking only
    from adaos.services.agent_context import AgentContext


def require_ctx(feature: Optional[str] = None) -> "AgentContext":
    """Return the active :class:`AgentContext` or raise a SDK-friendly error."""

    try:
        return get_ctx()
    except RuntimeError as exc:  # context not initialised yet
        raise SdkRuntimeNotInitialized(feature, str(exc)) from exc


__all__ = ["require_ctx"]
