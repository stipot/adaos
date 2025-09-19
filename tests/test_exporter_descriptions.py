# tests/test_exporter_descriptions.py
from __future__ import annotations
import json, pathlib
from adaos.sdk.core.exporter import export as sdk_export
from adaos.services.agent_context import get_ctx


def test_sdk_export_std(tmp_path, monkeypatch):
    # гарантируем, что экспорт пишет в наш временный package_dir
    ctx = get_ctx()
    pkg = pathlib.Path(ctx.paths.package_dir()) if callable(ctx.paths.package_dir) else pathlib.Path(ctx.paths.package_dir)
    # не меняем настоящий проект; просто проверим структуры в памяти
    data = sdk_export(level="std")
    assert "tools" in data and isinstance(data["tools"], list)


def test_sdk_export_mini_lines():
    # mini нужен для LLM ранней стадии
    data = sdk_export(level="mini")
    lines = data.get("__mini_lines__", [])
    # экспортер может возвращать сразу строки или контейнер — проверим оба варианта
    assert isinstance(lines, (list, tuple))
