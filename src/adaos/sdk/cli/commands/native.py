# -*- coding: utf-8 -*-
import typer
from typing import Optional
from pathlib import Path

from adaos.agent.audio.tts.native_tts import NativeTTS
from adaos.agent.audio.stt.vosk_stt import VoskSTT

app = typer.Typer(help="Нативные офлайн-команды (TTS/STT)")


@app.command("say")
def say(
    text: str,
    lang: Optional[str] = typer.Option(None, "--lang", "-l"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v"),
    rate: Optional[int] = typer.Option(None, "--rate"),
    volume: Optional[float] = typer.Option(None, "--volume"),
):
    tts = NativeTTS(voice=voice, rate=rate, volume=volume, lang_hint=(lang or "").lower() or None)
    tts.say(text)


@app.command("start")
def start(
    text_on_start: Optional[str] = typer.Option(None, "--greet"),
    model_path: Optional[str] = typer.Option(None, "--model", "-m"),
    model_zip: Optional[Path] = typer.Option(None, "--model-zip", help="Локальный путь к ZIP с моделью Vosk"),
    device: Optional[int] = typer.Option(None, "--device"),
    samplerate: int = typer.Option(16000, "--samplerate", "-r"),
    echo: bool = typer.Option(True, "--echo/--no-echo"),
    lang: Optional[str] = typer.Option(None, "--lang", "-l"),
):
    stt = VoskSTT(model_path=model_path, samplerate=samplerate, device=device, lang=lang, model_zip=model_zip)
    tts = NativeTTS(lang_hint=lang) if echo or text_on_start else None

    if text_on_start and tts:
        tts.say(text_on_start)

    def on_text(text: str):
        typer.echo(f"[ASR] {text}")
        if tts and echo:
            tts.say(text)

    stt.listen(on_text=on_text)
