"""Common SDK-side exceptions shared by control-plane facades."""

from __future__ import annotations

from typing import Optional

__all__ = [
    "SdkRuntimeNotInitialized",
    "CapabilityError",
    "QuotaExceeded",
    "ConflictError",
    "BusNotAvailable",
]


class SdkRuntimeNotInitialized(RuntimeError):
    """Raised when an SDK facade is invoked without an active AgentContext."""

    def __init__(self, message: str = "AdaOS SDK runtime is not initialized", *, detail: Optional[str] = None) -> None:
        self.detail = detail
        super().__init__(message if detail is None else f"{message}: {detail}")


class CapabilityError(PermissionError):
    """Raised when the current context lacks a required capability token."""

    def __init__(self, capability: str, *, message: str | None = None) -> None:
        self.capability = capability
        super().__init__(message or f"missing capability: {capability}")


class QuotaExceeded(RuntimeError):
    """Raised when a quota guard rejects an operation (KV / FS / NET)."""

    def __init__(self, resource: str | None = None, *, message: str | None = None) -> None:
        self.resource = resource
        text = message or (f"quota exceeded: {resource}" if resource else "quota exceeded")
        super().__init__(text)


class ConflictError(RuntimeError):
    """Raised when an idempotent operation detects a conflicting request id."""

    def __init__(self, request_id: str, *, message: str | None = None) -> None:
        self.request_id = request_id
        super().__init__(message or f"conflicting request_id: {request_id}")


class BusNotAvailable(RuntimeError):
    """Raised when an event bus operation is requested without a configured bus."""

    pass
