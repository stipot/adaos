"""Common SDK error hierarchy used across control/data planes."""

from __future__ import annotations

from typing import Optional


class SdkError(RuntimeError):
    """Base class for all SDK-level runtime errors."""


class SdkRuntimeNotInitialized(SdkError):
    """Raised when an SDK facade is invoked without an active :class:`AgentContext`."""

    def __init__(self, feature: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.feature = feature
        self.detail = detail
        message = "AdaOS SDK runtime is not initialized"
        if feature:
            message = f"{feature} requires AdaOS runtime context"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class CapabilityError(SdkError):
    """Raised when a capability required by the SDK facade is missing."""

    def __init__(self, capability: str, subject: Optional[str] = None) -> None:
        self.capability = capability
        self.subject = subject
        if subject:
            message = f"Capability '{capability}' is not granted for subject '{subject}'"
        else:
            message = f"Capability '{capability}' is not granted"
        super().__init__(message)


class QuotaExceeded(SdkError):
    """Raised when backend services signal a quota/usage limit violation."""

    def __init__(self, resource: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.resource = resource
        self.detail = detail
        message = "Quota exceeded"
        if resource:
            message = f"Quota exceeded for {resource}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class ConflictError(SdkError):
    """Raised when an idempotent mutation conflicts with existing state."""

    def __init__(self, message: str, *, detail: Optional[str] = None) -> None:
        self.detail = detail
        super().__init__(message if detail is None else f"{message}: {detail}")


__all__ = [
    "SdkError",
    "SdkRuntimeNotInitialized",
    "CapabilityError",
    "QuotaExceeded",
    "ConflictError",
]
