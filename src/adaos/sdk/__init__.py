# src/adaos/sdk/__init__.py
from __future__ import annotations
import warnings

# фасады
from .bus import emit, on
from .decorators import subscribe, register_subscriptions, tool, tools_registry, tools_meta, event_payload, emits
from .exporter import export
from .scenarios import (
    create as scenarios_create,
    install as scenarios_install,
    uninstall as scenarios_uninstall,
    pull as scenarios_pull,
    push as scenarios_push,
    list_installed as scenarios_list_installed,
    delete as scenarios_delete,
    read_proto as scenarios_read_proto,
    write_proto as scenarios_write_proto,
    read_bindings as scenarios_read_bindings,
    write_bindings as scenarios_write_bindings,
)

# публичные инструменты навыков
from .skills import (
    create,
    install,
    uninstall,
    pull,
    push,
    list_installed,
    install_all,
)

__all__ = [
    # bus
    "emit",
    "on",
    # decorators/registry
    "subscribe",
    "register_subscriptions",
    "tool",
    "tools_registry",
    "tools_meta",
    "event_payload",
    "emits",
    # exporter
    "export",
    # skills public
    "create",
    "install",
    "uninstall",
    "pull",
    "push",
    "list_installed",
    "install_all",
    # scenarios public
    "scenarios_create",
    "scenarios_install",
    "scenarios_uninstall",
    "scenarios_pull",
    "scenarios_push",
    "scenarios_list_installed",
    "scenarios_delete",
    "scenarios_read_proto",
    "scenarios_write_proto",
    "scenarios_read_bindings",
    "scenarios_write_bindings",
    # legacy aliases
    "create_skill",
    "install_skill",
    "uninstall_skill",
    "pull_skill",
    "push_skill",
    "list_installed_skills",
    "install_all_skills",
]


# ---- legacy aliases (backward compat) ----
def _deprecated(name: str, new: str):
    warnings.warn(f"adaos.sdk.{name} is deprecated; use adaos.sdk.{new}", DeprecationWarning, stacklevel=2)


def create_skill(name: str, template: str = "demo_skill") -> str:
    _deprecated("create_skill", "create")
    return create(name, template)


def install_skill(name: str) -> str:
    _deprecated("install_skill", "install")
    return install(name)


def uninstall_skill(name: str) -> str:
    _deprecated("uninstall_skill", "uninstall")
    return uninstall(name)


def pull_skill(name: str) -> str:
    _deprecated("pull_skill", "pull")
    return pull(name)


def push_skill(name: str, message: str, signoff: bool = False) -> str:
    _deprecated("push_skill", "push")
    return push(name, message, signoff=signoff)


def list_installed_skills() -> list[str]:
    _deprecated("list_installed_skills", "list_installed")
    return list_installed()


def install_all_skills(limit: int | None = None) -> list[str]:
    _deprecated("install_all_skills", "install_all")
    return install_all(limit)
