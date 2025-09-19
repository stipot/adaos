from __future__ import annotations

import importlib

from adaos.services.agent_context import clear_ctx


def test_sdk_imports_no_ctx():
    clear_ctx()
    modules = [
        "adaos.sdk.memory",
        "adaos.sdk.secrets",
        "adaos.sdk.fs",
        "adaos.sdk.validation.skill",
        "adaos.sdk.manage",
        "adaos.sdk.events",
        "adaos.sdk.i18n",
    ]
    for name in modules:
        importlib.import_module(name)
