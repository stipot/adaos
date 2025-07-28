from .a_permissions import require_permission
from voice.tts import tts

def speak(text, emotion="neutral", voice="anna"):
    require_permission("audio.speak")
    print(f"[TTS] {voice} - {emotion}: {text}")
    tts(text, emotion=emotion, voice=voice)