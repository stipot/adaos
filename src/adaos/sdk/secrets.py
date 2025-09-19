"""SDK facade for interacting with the secrets service."""

from __future__ import annotations

from typing import Optional

from ._caps import require_capability
from ._runtime import require_ctx


def read(name: str) -> Optional[str]:
    """Return a secret by name or ``None`` if it is not present."""

    ctx = require_ctx("sdk.secrets.read")
    require_capability(ctx, "secrets.read")
    return ctx.secrets.get(name)


def write(name: str, value: str) -> None:
    """Store or update a secret value."""

    ctx = require_ctx("sdk.secrets.write")
    require_capability(ctx, "secrets.write")
    ctx.secrets.put(name, value)


__all__ = ["read", "write"]
