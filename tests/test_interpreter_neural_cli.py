from __future__ import annotations

import json

from typer.testing import CliRunner


def test_neural_probe_joins_extra_text_words(monkeypatch):
    from adaos.apps.cli.commands import interpreter

    seen = {}

    async def _fake_parse_text(text, **kwargs):
        seen["text"] = text
        seen["kwargs"] = kwargs
        return {"ok": True, "accepted": True, "intent": "weather.get"}

    monkeypatch.setattr(interpreter.neural_service_bridge, "parse_text", _fake_parse_text)

    result = CliRunner().invoke(
        interpreter.app,
        ["neural-probe", "what", "is", "weather", "--locale", "en"],
    )

    assert result.exit_code == 0
    assert seen["text"] == "what is weather"
    assert seen["kwargs"]["locale"] == "en"
    assert json.loads(result.output)["intent"] == "weather.get"
