"""Minimal console output adapter used by the sandbox scenario runner."""

from __future__ import annotations

import os
import sys


def print(text: str | None) -> dict[str, bool]:
    """Write ``text`` to stdout and flush immediately."""

    if text is None:
        return {"ok": True}
    if os.getenv("ADAOS_TESTING"):
        return {"ok": True}
    sys.stdout.write(f"{text.rstrip()}\n")
    sys.stdout.flush()
    return {"ok": True}


__all__ = ["print"]
