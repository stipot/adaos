"""Capability guard helpers used by SDK control-plane facades."""

from __future__ import annotations

from ._ctx import require_ctx
from .errors import CapabilityError

__all__ = ["require_cap"]


def _allows(caps_obj, capability: str) -> bool:
    if hasattr(caps_obj, "allows"):
        try:
            return bool(caps_obj.allows(capability))
        except TypeError:
            return bool(caps_obj.allows("sdk", capability))
    if hasattr(caps_obj, "check"):
        try:
            return bool(caps_obj.check("sdk", [capability]))
        except TypeError:
            try:
                return bool(caps_obj.check([capability]))
            except TypeError:
                return bool(caps_obj.check(capability))
    if hasattr(caps_obj, "require"):
        try:
            caps_obj.require("sdk", capability)
            return True
        except TypeError:
            caps_obj.require(capability)
            return True
        except Exception:
            return False
    if hasattr(caps_obj, "has"):
        try:
            return bool(caps_obj.has("sdk", capability))
        except TypeError:
            return bool(caps_obj.has(capability))
    if hasattr(caps_obj, "__contains__"):
        return capability in caps_obj
    return False


def require_cap(*capabilities: str):
    """Ensure all capabilities are granted for the active context."""

    if not capabilities:
        raise ValueError("require_cap expects at least one capability token")

    ctx = require_ctx("Capability check requires runtime context")
    caps_obj = getattr(ctx, "caps", None)
    if caps_obj is None:
        raise CapabilityError(capabilities[0])

    for capability in capabilities:
        if not _allows(caps_obj, capability):
            raise CapabilityError(capability)
    return ctx
