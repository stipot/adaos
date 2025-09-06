# tests/conftest.py
from pathlib import Path
import os
import shutil
import pytest


@pytest.fixture
def tmp_base_dir(tmp_path, monkeypatch):
    base = tmp_path / ".adaos"
    (base / "skills").mkdir(parents=True)
    monkeypatch.setenv("ADAOS_BASE_DIR", str(base))
    # на всякий — HOME/USERPROFILE
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return base


@pytest.fixture
def cli_app():
    from adaos.app.cli.app import app

    return app
