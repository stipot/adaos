# tests/test_bus_sdk_adapter.py
from __future__ import annotations
import asyncio, json
from pathlib import Path
from adaos.sdk import bus
from adaos.services.agent_context import get_ctx


async def _once():
    await asyncio.sleep(0)


def test_bus_emit_and_on(tmp_path):
    ctx = get_ctx()
    seen = {}

    async def handler(payload: dict):
        seen.update(payload)

    # подписка
    asyncio.get_event_loop().run_until_complete(bus.on("unit.test", handler))

    # публикация с метаданными
    asyncio.get_event_loop().run_until_complete(bus.emit("unit.test", {"hello": "world"}, source="testcase", actor="pytest"))
    # даём ивентлупу тик
    asyncio.get_event_loop().run_until_complete(_once())

    # payload дошёл
    assert seen.get("hello") == "world"

    # ивент должен был залогироваться (setup_logging + attach_event_logger в conftest)
    log = Path(get_ctx().paths.logs_dir()) / "adaos.log"
    assert log.exists()
    # в последней строке провалидируем, что это JSON
    last = log.read_text(encoding="utf-8").strip().splitlines()[-1]
    evt = json.loads(last)
    assert evt["type"] in {"unit.test", "sys.ready", "sys.bus.ready"}  # хотя бы одно из
