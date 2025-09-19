# src/adaos/skills/weather_skill/tests/conftest.py
import shutil
from pathlib import Path
import pytest


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    """
    Копируем minimal prep внутрь tmp, чтобы тесты не трогали рабочую .adaos.
    """
    here = Path(__file__).resolve().parents[1]  # weather_skill/
    tmp_skill = tmp_path / "weather_skill"
    shutil.copytree(here, tmp_skill, dirs_exist_ok=True)
    return tmp_skill
