from __future__ import annotations

import importlib

from adaos.services.agent_context import clear_ctx


def test_sdk_imports_no_ctx():
    clear_ctx()
    modules = [
        "adaos.sdk.data.memory",
        "adaos.sdk.data.secrets",
        "adaos.sdk.data.fs",
        "adaos.sdk.core.validation.skill",
        "adaos.sdk.manage",
        "adaos.sdk.data.events",
        "adaos.sdk.data.i18n",
    ]
    for name in modules:
        importlib.import_module(name)
