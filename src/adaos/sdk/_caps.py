"""Backward compatible wrapper around the new capability helper."""

from __future__ import annotations

from ._cap import require_cap as require_cap


def require_capability(*caps: str):
    return require_cap(*caps)


__all__ = ["require_capability", "require_cap"]
