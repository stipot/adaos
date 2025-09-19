"""Utilities for capability checks within the SDK facades."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .context import get_current_skill
from .errors import CapabilityError


def _subject_candidates() -> Iterable[str]:
    skill = get_current_skill()
    if skill and getattr(skill, "name", None):
        sid = getattr(skill, "name")
        yield f"skill:{sid}"
        yield sid
    yield "sdk"
    yield "global"


def require_capability(ctx: Any, capability: str) -> None:
    """Ensure the current context grants the capability, or raise."""

    caps = getattr(ctx, "caps", None)
    if caps is None:
        raise CapabilityError(capability)

    last_error: Optional[Exception] = None
    # Prefer explicit allow/has checks; fall back to require semantics.
    for subject in _subject_candidates():
        if hasattr(caps, "allows"):
            try:
                allowed = caps.allows(capability)  # type: ignore[call-arg]
            except TypeError:
                allowed = caps.allows(subject, capability)  # type: ignore[call-arg]
            if allowed:
                return
            last_error = CapabilityError(capability, subject)
            continue

        if hasattr(caps, "has"):
            try:
                has_cap = caps.has(subject, capability)  # type: ignore[call-arg]
            except TypeError as exc:  # pragma: no cover - defensive
                last_error = exc
                continue
            if has_cap:
                return
            last_error = CapabilityError(capability, subject)
            continue

        if hasattr(caps, "require"):
            try:
                caps.require(subject, capability)  # type: ignore[call-arg]
            except Exception as exc:
                last_error = exc
                continue
            return

        break  # unknown capability interface

    if isinstance(last_error, CapabilityError):
        raise last_error
    if last_error is not None:
        raise CapabilityError(capability) from last_error
    raise CapabilityError(capability)


__all__ = ["require_capability"]
