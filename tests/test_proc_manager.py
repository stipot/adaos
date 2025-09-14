# tests/test_proc_manager.py
from __future__ import annotations
import sys, asyncio
from adaos.services.agent_context import get_ctx
from adaos.domain.types import ProcessSpec


async def _sleepy():
    await asyncio.sleep(0.05)


async def _run_cmd_and_check():
    ctx = get_ctx()
    cmd = [sys.executable, "-c", "import time; time.sleep(0.05)"]
    h = await ctx.proc.start(ProcessSpec(name="cmd", cmd=cmd))
    st1 = await ctx.proc.status(h)
    assert st1 in ("running", "stopped")  # может быстро завершиться
    await asyncio.sleep(0.1)
    st2 = await ctx.proc.status(h)
    assert st2 in ("stopped", "error")


async def _run_coro_and_check():
    ctx = get_ctx()
    h = await ctx.proc.start(ProcessSpec(name="coro", entrypoint=_sleepy))
    st1 = await ctx.proc.status(h)
    assert st1 in ("running", "stopped")
    await asyncio.sleep(0.1)
    st2 = await ctx.proc.status(h)
    assert st2 in ("stopped", "error")


def test_proc_flows(event_loop):
    event_loop.run_until_complete(_run_cmd_and_check())
    event_loop.run_until_complete(_run_coro_and_check())
