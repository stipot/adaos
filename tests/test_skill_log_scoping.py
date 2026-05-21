from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from adaos.adapters.fs.path_provider import PathProvider
from adaos.adapters.sdk.inproc_skill_context import InprocSkillContext
from adaos.services.agent_context import clear_ctx, set_ctx
from adaos.services.logging import setup_logging
from adaos.services.root_mcp.logs import list_local_logs
from adaos.services.status_card_registry import clear_status_card_registry
from adaos.services.ui_runtime_diagnostics import ingest_ui_runtime_diagnostics


def test_ui_runtime_diagnostics_write_skill_scoped_log_and_mcp_can_read_it(tmp_path: Path) -> None:
    clear_status_card_registry()
    paths = PathProvider(tmp_path)
    paths.ensure_tree()
    set_ctx(SimpleNamespace(paths=paths, skill_ctx=InprocSkillContext()))
    try:
        result = asyncio.run(
            ingest_ui_runtime_diagnostics(
                {
                    "webspace_id": "desktop",
                    "events": [
                        {
                            "level": "warning",
                            "source": "ui.modal",
                            "code": "modal.not_found",
                            "message": "Modal missing.",
                            "skillId": "browsers_skill",
                            "details": {"requestedId": "browser_link_settings_modal"},
                        }
                    ],
                }
            )
        )
        assert result["accepted"] == 1

        log_path = paths.skill_ui_diagnostics_log_path("browsers_skill")
        assert log_path.exists()
        line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        assert line["skill_id"] == "browsers_skill"
        assert line["code"] == "modal.not_found"
        assert line["webspace_id"] == "desktop"

        payload = list_local_logs(
            category="skills",
            skill="browsers_skill",
            logs_dir=paths.logs_dir(),
            lines=5,
        )
        assert [item["name"] for item in payload["items"]] == ["service.browsers_skill.ui_runtime.log"]
    finally:
        clear_status_card_registry()
        clear_ctx()


def test_ui_runtime_diagnostics_preserve_unattributed_fallback_log_name(tmp_path: Path) -> None:
    clear_status_card_registry()
    paths = PathProvider(tmp_path)
    paths.ensure_tree()
    set_ctx(SimpleNamespace(paths=paths, skill_ctx=InprocSkillContext()))
    try:
        result = asyncio.run(
            ingest_ui_runtime_diagnostics(
                {
                    "webspace_id": "desktop",
                    "events": [
                        {
                            "level": "warning",
                            "source": "ui.modal",
                            "code": "modal.missing_id",
                            "message": "Cannot open modal: modal id is missing.",
                            "details": {"options": {"nodeId": "node-1"}},
                        }
                    ],
                }
            )
        )
        assert result["accepted"] == 1

        log_path = paths.skill_ui_diagnostics_log_path("__ui_runtime__")
        assert log_path.name == "service.__ui_runtime__.ui_runtime.log"
        assert log_path.exists()
        line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        assert line["skill_id"] == "__ui_runtime__"
        assert line["code"] == "modal.missing_id"
    finally:
        clear_status_card_registry()
        clear_ctx()


def test_skill_context_logs_route_to_skill_runtime_log_not_platform_log(tmp_path: Path) -> None:
    paths = PathProvider(tmp_path)
    paths.ensure_tree()
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skill_ctx = InprocSkillContext()
    set_ctx(SimpleNamespace(paths=paths, skill_ctx=skill_ctx))
    adaos_logger = logging.getLogger("adaos")
    previous_handlers = list(adaos_logger.handlers)
    previous_level = adaos_logger.level
    previous_propagate = adaos_logger.propagate
    logger = setup_logging(paths, level="DEBUG")
    try:
        assert skill_ctx.set("demo_skill", skill_dir)
        logging.getLogger("adaos.demo.skill").warning("skill-only")
        skill_ctx.clear()
        logging.getLogger("adaos.demo.platform").warning("platform")
        for handler in logger.handlers:
            handler.flush()

        platform_log = paths.logs_dir() / "adaos.log"
        skill_log = paths.skill_runtime_log_path("demo_skill")
        assert "platform" in platform_log.read_text(encoding="utf-8")
        assert "skill-only" not in platform_log.read_text(encoding="utf-8")
        assert "skill-only" in skill_log.read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        clear_ctx()
