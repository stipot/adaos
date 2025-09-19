from __future__ import annotations


class SdkRuntimeNotInitialized(RuntimeError):
    """Raised when an SDK facade is used before the runtime context is available."""


class CapabilityError(PermissionError):
    """Raised when the current skill lacks required capabilities."""


class QuotaExceeded(RuntimeError):
    """Raised when an operation exceeds an allocated quota."""
