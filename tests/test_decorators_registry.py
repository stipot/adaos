# tests/test_decorators_registry.py
from __future__ import annotations
from adaos.sdk.core.decorators import tool, resolve_tool


def test_tool_registration_and_resolve():
    @tool("unit.echo")
    def echo(x: int) -> int:
        return x

    fn = resolve_tool(echo.__module__, "unit.echo")
    assert fn is echo
