# Interface deprecated
from .a_permissions import require_permission


def speak(text, emotion="neutral", voice="anna"):
    require_permission("audio.speak")
    print(f"[TTS] {voice} - {emotion}: {text}. Interface deprecated")
