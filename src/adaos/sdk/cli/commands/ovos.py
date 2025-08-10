import typer

# Обновлённый импорт с правильной фабрикой TTS
try:
    from adaos.integrations.ovos_adapter.tts import OVOSTTSAdapter
except ImportError as e:
    typer.echo(f"[OVOS Import Error] {e}")
    OVOSTTSAdapter = None

app = typer.Typer(help="OVOS интеграция")


@app.command("say")
def say(text: str):
    """Произнести фразу через OVOS TTS"""
    if OVOSTTSAdapter is None:
        typer.echo("Ошибка: TTS-адаптер OVOS не доступен.")
        raise typer.Exit(1)

    tts = OVOSTTSAdapter()
    tts.say(text)
