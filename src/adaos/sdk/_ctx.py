from __future__ import annotations

from adaos.services.agent_context import AgentContext, get_ctx

from adaos.sdk.errors import SdkRuntimeNotInitialized


def require_ctx(message: str) -> AgentContext:
    """Return the current :class:`AgentContext` or fail with an SDK-specific error."""
    try:
        return get_ctx()
    except RuntimeError as exc:  # pragma: no cover - defensive wrapper
        raise SdkRuntimeNotInitialized(message) from exc
