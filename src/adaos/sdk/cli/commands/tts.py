from __future__ import annotations

import os
import typer
from enum import Enum
from typing import Optional

app = typer.Typer(help="Унифицированные TTS-команды (OVOS / Rhasspy)")


# провайдеры через Enum вместо typing.Literal
class ProviderEnum(str, Enum):
    auto = "auto"
    ovos = "ovos"
    rhasspy = "rhasspy"


# мягкие импорты провайдеров
try:
    from adaos.integrations.ovos_adapter.tts import OVOSTTSAdapter  # type: ignore
except Exception:
    OVOSTTSAdapter = None  # type: ignore

try:
    from adaos.integrations.rhasspy_adapter.tts import RhasspyTTSAdapter  # type: ignore
except Exception:
    RhasspyTTSAdapter = None  # type: ignore


def _pick_provider(provider: ProviderEnum, rhasspy_url: str) -> ProviderEnum:
    if provider == ProviderEnum.auto:
        if rhasspy_url or os.getenv("ADAOS_RHASSPY_URL"):
            return ProviderEnum.rhasspy
        return ProviderEnum.ovos
    return provider


@app.command("say")
def say(
    text: str,
    provider: ProviderEnum = typer.Option(ProviderEnum.auto, "--provider", case_sensitive=False),
    ovos_voice: str = typer.Option(None, "--ovos-voice"),
    rhasspy_url: str = typer.Option(None, "--rhasspy-url"),
    rhasspy_voice: str = typer.Option(None, "--rhasspy-voice"),
    rhasspy_lang: str = typer.Option(None, "--rhasspy-lang"),
):
    """Произнести фразу через выбранный провайдер TTS."""
    effective = _pick_provider(provider, rhasspy_url)

    if effective == ProviderEnum.ovos:
        if OVOSTTSAdapter is None:
            typer.echo("OVOS не установлен/не сконфигурирован (нужны ovos_plugin_manager, ovos_config и mycroft.conf).")
            raise typer.Exit(1)
        OVOSTTSAdapter(override_voice=ovos_voice).say(text)
        return

    if effective == ProviderEnum.rhasspy:
        if RhasspyTTSAdapter is None:
            typer.echo("Rhasspy-адаптер недоступен.")
            raise typer.Exit(1)
        RhasspyTTSAdapter(base_url=rhasspy_url, voice=rhasspy_voice, lang=rhasspy_lang).say(text)
        return

    typer.echo("Неизвестный провайдер")
    raise typer.Exit(1)
