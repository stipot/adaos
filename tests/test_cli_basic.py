# tests/test_cli_basic.py
from typer.testing import CliRunner


def test_cli_help(cli_app):
    r = CliRunner().invoke(cli_app, ["--help"])
    assert r.exit_code == 0
    assert "Usage" in r.stdout or "использование" in r.stdout.lower()
