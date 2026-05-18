from __future__ import annotations

import json
import os
from pathlib import Path

from adaos.apps.cli import app as cli_app


def test_active_slot_manifest_payload_prefers_active_slot_venv(monkeypatch, tmp_path: Path) -> None:
    base_dir = tmp_path / ".adaos"
    slot_dir = base_dir / "state" / "core_slots" / "slots" / "B"
    repo_dir = slot_dir / "repo"
    venv_dir = slot_dir / "venv"
    src_dir = repo_dir / "src"
    python_bin = venv_dir / "bin" / "python"
    manifest_path = slot_dir / "manifest.json"

    (base_dir / "state" / "core_slots").mkdir(parents=True, exist_ok=True)
    (base_dir / "state" / "core_slots" / "active").write_text("B\n", encoding="utf-8")
    src_dir.mkdir(parents=True, exist_ok=True)
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "repo_dir": str(repo_dir),
                "cwd": str(repo_dir),
                "venv_dir": str(venv_dir),
                "env": {
                    "ADAOS_SLOT_REPO_ROOT": str(repo_dir),
                    "ADAOS_BASE_DIR": str(base_dir),
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    monkeypatch.setenv("PYTHONPATH", "/existing/pythonpath")

    python, env_map, resolved_repo_dir = cli_app._active_slot_manifest_payload()

    assert python == str(python_bin)
    assert resolved_repo_dir == str(repo_dir)
    assert env_map["ADAOS_BASE_DIR"] == str(base_dir)
    assert env_map["ADAOS_ACTIVE_CORE_SLOT"] == "B"
    assert env_map["ADAOS_ACTIVE_CORE_SLOT_DIR"] == str(slot_dir)
    assert env_map["ADAOS_SLOT_REPO_ROOT"] == str(repo_dir)
    assert env_map["PYTHONPATH"] == f"{src_dir}{os.pathsep}/existing/pythonpath"


def test_should_reexec_active_slot_venv_when_current_python_differs(monkeypatch, tmp_path: Path) -> None:
    base_dir = tmp_path / ".adaos"
    slot_dir = base_dir / "state" / "core_slots" / "slots" / "A"
    venv_dir = slot_dir / "venv"
    python_bin = venv_dir / "bin" / "python"
    manifest_path = slot_dir / "manifest.json"

    (base_dir / "state" / "core_slots").mkdir(parents=True, exist_ok=True)
    (base_dir / "state" / "core_slots" / "active").write_text("A\n", encoding="utf-8")
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "repo_dir": str(slot_dir / "repo"),
                "cwd": str(slot_dir / "repo"),
                "venv_dir": str(venv_dir),
                "env": {},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    monkeypatch.delenv("ADAOS_CLI_REEXECED", raising=False)
    monkeypatch.delenv("ADAOS_DISABLE_ACTIVE_SLOT_PYTHON_REEXEC", raising=False)
    monkeypatch.setattr(cli_app.sys, "executable", str(tmp_path / "other-python"))

    assert cli_app._should_reexec_active_slot_venv() is True


def test_apply_active_slot_manifest_environment_when_python_already_slot_bound(monkeypatch, tmp_path: Path) -> None:
    base_dir = tmp_path / ".adaos"
    slot_dir = base_dir / "state" / "core_slots" / "slots" / "B"
    repo_dir = slot_dir / "repo"
    venv_dir = slot_dir / "venv"
    src_dir = repo_dir / "src"
    python_bin = venv_dir / "bin" / "python"
    manifest_path = slot_dir / "manifest.json"

    (base_dir / "state" / "core_slots").mkdir(parents=True, exist_ok=True)
    (base_dir / "state" / "core_slots" / "active").write_text("B\n", encoding="utf-8")
    src_dir.mkdir(parents=True, exist_ok=True)
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "repo_dir": str(repo_dir),
                "cwd": str(repo_dir),
                "venv_dir": str(venv_dir),
                "env": {},
            }
        ),
        encoding="utf-8",
    )

    start_dir = tmp_path / "start"
    start_dir.mkdir()
    monkeypatch.chdir(start_dir)
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
    monkeypatch.delenv("ADAOS_ACTIVE_CORE_SLOT", raising=False)
    monkeypatch.delenv("ADAOS_ACTIVE_CORE_SLOT_DIR", raising=False)
    monkeypatch.delenv("ADAOS_SLOT_REPO_ROOT", raising=False)
    monkeypatch.delenv("ADAOS_CLI_SLOT_BOUND", raising=False)
    monkeypatch.setattr(cli_app.sys, "executable", str(python_bin))

    assert cli_app._apply_active_slot_manifest_environment_if_current() is True

    assert os.environ["ADAOS_ACTIVE_CORE_SLOT"] == "B"
    assert os.environ["ADAOS_ACTIVE_CORE_SLOT_DIR"] == str(slot_dir)
    assert os.environ["ADAOS_SLOT_REPO_ROOT"] == str(repo_dir)
    assert os.environ["ADAOS_CLI_SLOT_BOUND"] == "1"
    assert Path.cwd() == repo_dir


def test_repo_venv_reexec_is_disabled_after_slot_binding(monkeypatch) -> None:
    monkeypatch.setenv("ADAOS_CLI_SLOT_BOUND", "1")
    monkeypatch.delenv("ADAOS_CLI_REEXECED", raising=False)

    assert cli_app._should_reexec_repo_venv() is False
