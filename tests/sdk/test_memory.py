from __future__ import annotations

from pathlib import Path

from adaos.sdk.data import memory
from adaos.services.agent_context import get_ctx


def test_memory_namespace(tmp_path):
    ctx = get_ctx()

    skill_dir = Path(ctx.paths.skills_dir()) / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    assert ctx.skill_ctx.set("demo-skill", skill_dir)

    try:
        memory.put("runs", 1)
        assert ctx.kv.get("skills/demo-skill/runs") == 1
        assert memory.get("runs") == 1

        keys = ctx.kv.list(prefix="skills/demo-skill/")
        assert "skills/demo-skill/runs" in keys

        listed = memory.list()
        assert "runs" in listed
    finally:
        ctx.skill_ctx.clear()
