# -*- coding: utf-8 -*-
import typer, sys, os
from typing import Optional
from pathlib import Path

from adaos.adapters.audio.tts.native_tts import NativeTTS

VoskSTT = None  # ленивый импорт
from adaos.adapters.audio.stt.vosk_stt import VoskSTT

app = typer.Typer(help="Native (audio) commands")


def _is_android() -> bool:
    return sys.platform == "android" or "ANDROID_ARGUMENT" in os.environ


def _try_start_android_service():
    # Стартуем Foreground Service через Java-интент
    try:
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        context = PythonActivity.mActivity
        service_cls = autoclass("ai.adaos.platform.android.AdaOSAudioService")
        intent = Intent(context, service_cls)
        context.startForegroundService(intent)
        return True
    except Exception as e:
        typer.echo(f"[AndroidService] failed: {e}")
        return False


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

from adaos.adapters.audio.stt.vosk_stt import VoskSTT


@app.command("start")
def start(
    lang: str = typer.Option("en", "--lang", help="Язык"),
    samplerate: int = typer.Option(16000, "--samplerate", help="Частота дискретизации"),
    device: str = typer.Option(None, "--device", help="Устройство"),
    echo: bool = typer.Option(True, "--echo", help="Эхо-тест"),
    model_path: str = typer.Option(None, "--model-path", help="Путь к модели"),
    use_android_service: bool = True,
):
    """
    Запускает офлайн-слушатель (Vosk). Ctrl+C для выхода.
    """
    external = None
    if use_android_service and _is_android():
        ok = _try_start_android_service()
        if ok:
            from adaos.platform.android.mic_udp import AndroidMicUDP

            external = AndroidMicUDP().listen_stream()
    global VoskSTT
    if VoskSTT is None:
        from adaos.adapters.audio.stt.vosk_stt import VoskSTT
    stt = VoskSTT(model_path=model_path, samplerate=samplerate, device=device, lang=lang)

    tts = None
    if echo:
        try:
            # Android TTS (если запускаемся внутри APK)
            from adaos.platform.android.android_tts import AndroidTTS

            tts = AndroidTTS(lang_hint="en-US" if lang.startswith("en") else "ru-RU")
        except Exception:
            # Десктоп/фоллбек
            from adaos.adapters.audio.tts.native_tts import NativeTTS

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
