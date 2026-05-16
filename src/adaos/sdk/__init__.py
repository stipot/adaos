"""AdaOS SDK public facade."""

from __future__ import annotations

from importlib import import_module

__all__ = ["control_plane", "data", "manage", "status", "web", "validate_self"]


def __getattr__(name: str):
    if name in ("control_plane", "data", "manage", "status", "web"):
        return import_module(f"{__name__}.{name}")
    if name == "validate_self":
        from .core.validation.skill import validate_self

        return validate_self
    raise AttributeError(name)
