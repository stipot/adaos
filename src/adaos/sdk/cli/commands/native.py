# -*- coding: utf-8 -*-
import typer
from typing import Optional

from adaos.agent.audio.tts.native_tts import NativeTTS
from adaos.agent.audio.stt.vosk_stt import VoskSTT

app = typer.Typer(help="Нативные офлайн-команды (TTS/STT)")


# ---------- adaos say "Hello world!" ----------
@app.command("say")
def say(
    text: str = typer.Argument(..., help="Текст для озвучивания"),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="Подсказка языка для выбора TTS‑голоса (ru/en)"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v", help="Подстрока для фильтрации голоса"),
    rate: Optional[int] = typer.Option(None, "--rate", help="Скорость речи (слов/мин)"),
    volume: Optional[float] = typer.Option(None, "--volume", help="Громкость 0.0..1.0"),
):
    """
    Произнести текст офлайн через pyttsx3.
    """
    # Нормализация lang для голосов
    lang_hint = (lang or "").lower() or None
    tts = NativeTTS(voice=voice, rate=rate, volume=volume, lang_hint=lang_hint)
    tts.say(text)


# ---------- adaos start ----------
@app.command("start")
def start(
    text_on_start: Optional[str] = typer.Option(None, "--greet", help="Произнести приветствие при запуске"),
    model_path: Optional[str] = typer.Option(None, "--model", "-m", help="Путь к модели Vosk (иначе авто по --lang или ADAOS_VOSK_MODEL)"),
    device: Optional[int] = typer.Option(None, "--device", help="Индекс микрофона (sounddevice)"),
    samplerate: int = typer.Option(16000, "--samplerate", "-r", help="Частота дискретизации"),
    echo: bool = typer.Option(True, "--echo/--no-echo", help="Озвучивать распознанный текст"),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="Язык распознавания: en/ru (при отсутствии модели — авто‑загрузка)"),
):
    """
    Запускает офлайн‑слушатель (Vosk). Поддерживает авто‑загрузку модели по --lang.
    Остановить: Ctrl+C.
    """
    stt = VoskSTT(model_path=model_path, samplerate=samplerate, device=device, lang=lang)
    tts = NativeTTS(lang_hint=lang) if echo or text_on_start else None

    if text_on_start and tts:
        tts.say(text_on_start)

    def on_text(text: str):
        typer.echo(f"[ASR] {text}")
        if tts and echo:
            tts.say(text)

    stt.listen(on_text=on_text)
