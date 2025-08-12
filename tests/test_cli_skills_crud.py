# tests/test_cli_skills_crud.py
import json
from typer.testing import CliRunner


def test_skill_crud_flow(cli_app, tmp_base_dir, tmp_path):
    runner = CliRunner()

    # create
    r = runner.invoke(cli_app, ["skill", "create", "demo_skill_test", "--template", "demo_skill"])
    assert r.exit_code == 0
    assert "demo_skill" in r.stdout.lower()

    """
    # list
    r = runner.invoke(cli_app, ["skill", "list"])
    assert r.exit_code == 0
    assert "demo_skill_test" in r.stdout

    # uninstall
    r = runner.invoke(cli_app, ["skill", "uninstall", "demo_skill_test"])
    assert "demo_skill_test" not in r.stdout """

    # install (если у вас есть реестр/источник — адаптируйте; тут предположим локальную установку)
    r = runner.invoke(cli_app, ["skill", "install", "weather_skill"])
    assert r.exit_code == 0

    # uninstall
    r = runner.invoke(cli_app, ["skill", "uninstall", "weather_skill"])
    assert r.exit_code == 0

    # list — не должен содержать
    r = runner.invoke(cli_app, ["skill", "list"])
    assert r.exit_code == 0
    assert "weather_skill" not in r.stdout
