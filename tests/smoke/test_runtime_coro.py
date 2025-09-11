# tests/smoke/test_runtime_coro.py
import asyncio
import pytest
from adaos.services.agent_context import get_ctx
from adaos.domain import ProcessSpec


async def _demo_task():
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_coro_start_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
    ctx = init_ctx()
    h = await ctx.proc.start(ProcessSpec(name="demo-coro", entrypoint=_demo_task))
    st = await ctx.proc.status(h)
    assert st in ("starting", "running")  # мгновенный старт может быть ещё starting
    await asyncio.sleep(0.2)
    assert (await ctx.proc.status(h)) in ("stopped", "error")
