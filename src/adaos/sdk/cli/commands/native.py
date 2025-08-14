# -*- coding: utf-8 -*-
import typer
from typing import Optional
from pathlib import Path

VoskSTT = None  # ленивый импорт
from adaos.agent.audio.stt.vosk_stt import VoskSTT

app = typer.Typer(help="Native (audio) commands")


@app.command("say")
def say(
    text: str = typer.Argument(..., help="Текст для озвучивания"),
    lang: str = typer.Option(None, "--lang", "-l", help="Язык"),
    voice: str = typer.Option(None, "--voice", "-v", help="Голос"),
    rate: int = typer.Option(None, "--rate", help="Скорость речи"),
    volume: float = typer.Option(None, "--volume", help="Громкость [0..1]"),
):
    tts = NativeTTS(voice=voice, rate=rate, volume=volume, lang_hint=(lang or "").lower() or None)
    tts.say(text)


import typer

from adaos.agent.audio.stt.vosk_stt import VoskSTT


@app.command("start")
def start(
    lang: str = typer.Option("en", "--lang", help="Язык"),
    samplerate: int = typer.Option(16000, "--samplerate", help="Частота дискретизации"),
    device: str = typer.Option(None, "--device", help="Устройство"),
    echo: bool = typer.Option(True, "--echo", help="Эхо-тест"),
    model_path: str = typer.Option(None, "--model-path", help="Путь к модели"),
):
    """
    Запускает офлайн-слушатель (Vosk). Ctrl+C для выхода.
    """
    global VoskSTT
    if VoskSTT is None:
        from adaos.agent.audio.stt.vosk_stt import VoskSTT
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
