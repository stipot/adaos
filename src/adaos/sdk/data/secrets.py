"""SDK facade for interacting with the secrets service."""

from __future__ import annotations

from typing import Optional

from adaos.sdk.core._cap import require_cap


def read(name: str) -> Optional[str]:
    """Return a secret by name or ``None`` if it is not present."""

    ctx = require_cap("secrets.read")
    return ctx.secrets.get(name)


def write(name: str, value: str) -> None:
    """Store or update a secret value."""

    ctx = require_cap("secrets.write")
    ctx.secrets.put(name, value)


__all__ = ["read", "write"]
