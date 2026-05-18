from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

if "y_py" not in sys.modules:
    sys.modules["y_py"] = types.SimpleNamespace(YDoc=object)
if "ypy_websocket" not in sys.modules:
    ystore_mod = types.SimpleNamespace(BaseYStore=object, YDocNotFound=RuntimeError)
    sys.modules["ypy_websocket"] = types.SimpleNamespace(ystore=ystore_mod)
    sys.modules["ypy_websocket.ystore"] = ystore_mod

from adaos.apps.cli.commands import skill as skill_cmd


def test_skill_push_rejoins_split_message(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    skill_dir = tmp_path / "demo_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_cmd, "_resolve_skill_path", lambda target: skill_dir)

    class _Mgr:
        def push(self, skill_name: str, message: str, signoff: bool = False) -> str:
            assert skill_name == "demo_skill"
            assert message == "initial commit"
            assert signoff is False
            return "rev-1"

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    result = runner.invoke(skill_cmd.app, ["push", "demo_skill", "--message", "initial", "commit"])
    assert result.exit_code == 0, result.output
    assert "done" in result.output.lower() or "rev-1" in result.output


def test_skill_push_without_message_releases_changed_skills(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    pushed: list[tuple[str, str, bool]] = []

    class _Mgr:
        def push(self, skill_name: str, message: str, signoff: bool = False) -> str:
            pushed.append((skill_name, message, signoff))
            return f"rev-{skill_name}"

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: _Mgr())
    monkeypatch.setattr(
        skill_cmd,
        "_collect_skill_release_candidates",
        lambda **kwargs: {
            "base_ref": "origin/main",
            "ahead_count": 2,
            "behind_count": 0,
            "skills": [
                {"name": "browsers_skill", "reasons": ["git-ahead"]},
                {"name": "infrastate_skill", "reasons": ["registry-version"]},
            ],
        },
    )

    result = runner.invoke(skill_cmd.app, ["push", "--signoff"])

    assert result.exit_code == 0, result.output
    assert pushed == [
        ("browsers_skill", "chore(browsers_skill): release workspace changes", True),
        ("infrastate_skill", "chore(infrastate_skill): release workspace changes", True),
    ]
    assert "released skill changes" in result.output
    assert "browsers_skill, infrastate_skill" in result.output


def test_skill_push_without_message_reports_when_named_skill_has_no_release_changes(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    pushed: list[str] = []

    monkeypatch.setattr(skill_cmd, "_mgr", lambda: SimpleNamespace(push=lambda *args, **kwargs: pushed.append("push")))
    monkeypatch.setattr(
        skill_cmd,
        "_collect_skill_release_candidates",
        lambda **kwargs: {
            "base_ref": "origin/main",
            "ahead_count": 1,
            "behind_count": 0,
            "skills": [],
        },
    )

    result = runner.invoke(skill_cmd.app, ["push", "infrastate_skill"])

    assert result.exit_code == 0, result.output
    assert pushed == []
    assert "infrastate_skill has no release changes" in result.output


def test_skill_push_message_requires_skill_name(monkeypatch) -> None:
    result = CliRunner().invoke(skill_cmd.app, ["push", "--message", "publish"])

    assert result.exit_code == 2
    assert "skill name is required" in result.output
