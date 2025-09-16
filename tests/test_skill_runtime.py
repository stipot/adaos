"""Tests for the reusable skill runtime helpers."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from adaos.services.agent_context import get_ctx
from adaos.services.skill.runtime import (
    SkillDirectoryNotFoundError,
    SkillPrepScriptNotFoundError,
    find_skill_dir,
    run_skill_handler_sync,
    run_skill_prep,
)


@pytest.fixture
def skill_factory() -> Callable[[str], Path]:
    """Create skills under the current ``skills_dir`` for runtime tests."""

    def _create_skill(
        name: str,
        *,
        handler_source: str | None = None,
        prep_source: str | None = textwrap.dedent(
            """
            from pathlib import Path

            def run_prep(skill_path: Path):
                return {"status": "ok"}
            """
        ),
    ) -> Path:
        ctx = get_ctx()
        root = Path(ctx.paths.skills_dir())
        skill_dir = root / name
        (skill_dir / "handlers").mkdir(parents=True, exist_ok=True)
        (skill_dir / "manifest.yaml").write_text(
            textwrap.dedent(
                f"""
                id: {name}
                name: {name}
                version: 0.0.1
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        handler_source = handler_source or textwrap.dedent(
            """
            def handle(topic, payload):
                return {"topic": topic, "payload": payload}
            """
        )
        (skill_dir / "handlers" / "main.py").write_text(handler_source, encoding="utf-8")

        if prep_source is not None:
            (skill_dir / "prep").mkdir(parents=True, exist_ok=True)
            (skill_dir / "prep" / "prepare.py").write_text(prep_source, encoding="utf-8")

        return skill_dir

    return _create_skill


def test_find_skill_dir_returns_direct_path(skill_factory):
    skill_dir = skill_factory("demo_skill")
    assert find_skill_dir("demo_skill").resolve() == skill_dir.resolve()


def test_find_skill_dir_missing_raises():
    with pytest.raises(SkillDirectoryNotFoundError):
        find_skill_dir("does_not_exist")


def test_run_skill_handler_sync_handles_coroutines(skill_factory):
    handler_source = textwrap.dedent(
        """
        import asyncio

        async def handle(topic, payload):
            await asyncio.sleep(0)
            return {"topic": topic, "payload": payload, "status": "ok"}
        """
    )
    skill_factory("async_skill", handler_source=handler_source, prep_source=None)

    result = run_skill_handler_sync("async_skill", "demo.topic", {"foo": "bar"})
    assert result == {"topic": "demo.topic", "payload": {"foo": "bar"}, "status": "ok"}


def test_run_skill_prep_executes_script(skill_factory):
    prep_source = textwrap.dedent(
        """
        from pathlib import Path

        def run_prep(skill_path: Path):
            artifact = skill_path / "prep" / "artifact.txt"
            artifact.write_text("done", encoding="utf-8")
            return {"status": "ok", "artifact": str(artifact)}
        """
    )
    skill_dir = skill_factory("prep_skill", prep_source=prep_source)

    result = run_skill_prep("prep_skill")

    assert result["status"] == "ok"
    assert Path(result["artifact"]).read_text(encoding="utf-8") == "done"
    assert (skill_dir / "prep" / "artifact.txt").exists()


def test_run_skill_prep_missing_script_raises(skill_factory):
    skill_factory("no_prep", prep_source=None)

    with pytest.raises(SkillPrepScriptNotFoundError):
        run_skill_prep("no_prep")
