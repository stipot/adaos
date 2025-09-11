# tests/smoke/test_runtime_cmd.py
import sys, shutil, asyncio, pytest
from adaos.services.agent_context import get_ctx
from adaos.domain import ProcessSpec

pytestmark = pytest.mark.asyncio


async def test_cmd_start(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
    ctx = init_ctx()
    cmd = [sys.executable, "-c", "print('hi')"]
    h = await ctx.proc.start(ProcessSpec(name="demo-cmd", cmd=cmd))
    await asyncio.sleep(0.1)
    # процесс должен завершиться сам и дать exited
    await asyncio.sleep(0.2)
    assert (await ctx.proc.status(h)) in ("stopped", "error")
