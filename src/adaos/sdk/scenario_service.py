# src/adaos/sdk/scenario_service.py
from __future__ import annotations
import warnings
from typing import Any, Dict, List, Optional

# проксируем в новый фасад
from adaos.sdk.scenarios import (
    create,
    install,
    uninstall,
    pull,
    push,
    list_installed,
    delete,
    read_proto,
    write_proto,
    read_bindings,
    write_bindings,
)

__all__ = [
    "create_scenario",
    "install_scenario",
    "uninstall_scenario",
    "pull_scenario",
    "push_scenario",
    "list_installed_scenarios",
    "delete_scenario",
    "read_prototype",
    "write_prototype",
    "read_bindings",
    "write_bindings",
]


def _dep(old: str, new: str):
    warnings.warn(f"adaos.sdk.{old} is deprecated; use adaos.sdk.scenarios.{new}", DeprecationWarning, stacklevel=2)


def create_scenario(sid: str, template: str = "template"):
    _dep("create_scenario", "create")
    return create(sid, template)


def install_scenario(sid: str):
    _dep("install_scenario", "install")
    return install(sid)


def uninstall_scenario(sid: str):
    _dep("uninstall_scenario", "uninstall")
    return uninstall(sid)


def pull_scenario(sid: str):
    _dep("pull_scenario", "pull")
    return pull(sid)


def push_scenario(sid: str, message: Optional[str] = None):
    _dep("push_scenario", "push")
    return push(sid, message=message)


def list_installed_scenarios() -> List[str]:
    _dep("list_installed_scenarios", "list_installed")
    return list_installed()


def delete_scenario(sid: str) -> bool:
    _dep("delete_scenario", "delete")
    return delete(sid)


# алиасы имён под старый интерфейс
def read_prototype(sid: str) -> Dict[str, Any]:
    _dep("read_prototype", "read_proto")
    return read_proto(sid)


def write_prototype(sid: str, data: Dict[str, Any]) -> str:
    _dep("write_prototype", "write_proto")
    return write_proto(sid, data)
