"""Utilities for capability checks within the SDK facades."""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from ._ctx import require_ctx
from .errors import CapabilityError


def _subject_candidates(ctx: Any) -> Iterable[str]:
    """Yield capability subjects to probe for the current context."""

    skill_ctx = getattr(ctx, "skill_ctx", None)
    if skill_ctx is not None:
        try:
            current = skill_ctx.get()
        except Exception:  # pragma: no cover - defensive
            current = None
        if current is not None:
            sid = getattr(current, "name", None)
            if isinstance(sid, str) and sid:
                yield f"skill:{sid}"
                yield sid
    actor = getattr(ctx, "actor", None)
    if isinstance(actor, str) and actor:
        yield actor
    # fallbacks
    yield "sdk"
    yield "core"
    yield "global"


def _allows(caps: Any, subject: str, capability: str) -> Optional[bool]:
    if hasattr(caps, "allows"):
        try:
            return bool(caps.allows(subject, capability))  # type: ignore[attr-defined]
        except TypeError:
            try:
                return bool(caps.allows(capability))  # type: ignore[misc]
            except TypeError:
                return None
    if hasattr(caps, "has"):
        try:
            return bool(caps.has(subject, capability))  # type: ignore[attr-defined]
        except TypeError:
            return None
    if hasattr(caps, "check"):
        try:
            return bool(caps.check(subject, (capability,)))  # type: ignore[attr-defined]
        except TypeError:
            try:
                return bool(caps.check((capability,)))  # type: ignore[misc]
            except TypeError:
                return None
    return None


def _require(caps: Any, subject: str, capability: str) -> bool | Exception:
    if hasattr(caps, "require"):
        try:
            caps.require(subject, capability)  # type: ignore[attr-defined]
            return True
        except Exception as exc:  # pragma: no cover - propagate later
            return exc
    return False


def require_cap(*capabilities: str) -> Any:
    """Ensure the active context grants all ``capabilities`` and return it."""

    ctx = require_ctx("capability check")
    if not capabilities:
        return ctx

    caps = getattr(ctx, "caps", None)
    if caps is None:
        raise CapabilityError(capabilities[0])

    subjects: Sequence[str] = tuple(_subject_candidates(ctx))
    for capability in capabilities:
        last_error: Optional[Exception] = None
        for subject in subjects:
            allowed = _allows(caps, subject, capability)
            if allowed is True:
                last_error = None
                break
            if allowed is False:
                last_error = CapabilityError(capability, subject)
                continue
            # fall back to require semantics
            res = _require(caps, subject, capability)
            if res is True:
                last_error = None
                break
            if isinstance(res, Exception):
                last_error = res
                continue
        else:  # no break
            if isinstance(last_error, CapabilityError):
                raise last_error
            if last_error is not None:
                raise CapabilityError(capability) from last_error
            raise CapabilityError(capability)
    return ctx


__all__ = ["require_cap"]
