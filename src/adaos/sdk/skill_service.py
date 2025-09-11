# src/adaos/sdk/skill_service.py
# Adopted
from __future__ import annotations
from typing import Optional, List
from adaos.sdk.skills import (
    create as _create,
    install as _install,
    uninstall as _uninstall,
    pull as _pull,
    push as _push,
    list_installed as _list_installed,
    install_all as _install_all,
)


# старые имена
def create_skill(name: str, template: str = "demo_skill") -> str:
    return _create(name, template)


def install_skill(name: str) -> str:
    return _install(name)


def uninstall_skill(name: str) -> str:
    return _uninstall(name)


def pull_skill(name: str) -> str:
    return _pull(name)


def push_skill(name: str, message: str, signoff: bool = False) -> str:
    return _push(name, message, signoff=signoff)


def list_installed_skills() -> List[str]:
    return _list_installed()


def install_all_skills(limit: Optional[int] = None) -> List[str]:
    return _install_all(limit)
