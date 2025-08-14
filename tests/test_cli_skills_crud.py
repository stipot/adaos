# tests/test_cli_skills_crud.py
import json
from typer.testing import CliRunner


def _names_from_list_json(cli_app):
    r = CliRunner().invoke(cli_app, ["skill", "list", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.stdout or "{}")
    return {item["name"] for item in data.get("skills", [])}


def test_skill_crud_flow(cli_app, tmp_base_dir, tmp_path):
    runner = CliRunner()

    # create локального демо-шаблона
    r = runner.invoke(cli_app, ["skill", "create", "demo_skill_test", "--template", "demo_skill"])
    assert r.exit_code == 0

    # install (предустановленный навык из монорепо)
    r = runner.invoke(cli_app, ["skill", "install", "weather_skill"])
    assert r.exit_code == 0

    # uninstall
    r = runner.invoke(cli_app, ["skill", "uninstall", "weather_skill"])
    assert r.exit_code == 0

    # проверяем отсутствия ТОЛЬКО точного имени
    names = _names_from_list_json(cli_app)
    assert "weather_skill" not in names
