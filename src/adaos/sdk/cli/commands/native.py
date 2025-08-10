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


import typer

from adaos.agent.audio.stt.vosk_stt import VoskSTT


@app.command("start")
def start(lang: str = "en", samplerate: int = 16000, device: str | int | None = None, echo: bool = True, model_path: str | None = None):
    """
    Запускает офлайн-слушатель (Vosk). Ctrl+C для выхода.
    """
    stt = VoskSTT(model_path=model_path, samplerate=samplerate, device=device, lang=lang)

    tts = None
    if echo:
        try:
            # Android TTS (если запускаемся внутри APK)
            from adaos.platform.android.android_tts import AndroidTTS

            tts = AndroidTTS(lang_hint="en-US" if lang.startswith("en") else "ru-RU")
        except Exception:
            # Десктоп/фоллбек
            from adaos.agent.audio.tts.native_tts import NativeTTS

            tts = NativeTTS(lang_hint=lang)

    typer.echo("[Vosk] Ready. Say something...")

    try:
        for phrase in stt.listen_stream():
            typer.echo(f"[STT] {phrase}")
            if tts:
                try:
                    tts.say(phrase)  # Говорим каждую итоговую фразу
                except Exception as e:
                    typer.echo(f"[TTS error] {e}")
    except KeyboardInterrupt:
        pass
    finally:
        stt.close()
