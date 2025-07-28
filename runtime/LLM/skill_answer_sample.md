--- manifest.yaml ---
name: AlarmSkill
version: 1.0
description: Навык для установки и отмены будильника.
permissions:
  - audio.speak
  - alarm.set
  - alarm.cancel
intents:
  - set_alarm
  - cancel_alarm
--- handler.py ---
from sdk import speak, set_alarm, cancel_alarm

def handle(intent, entities):
    if intent == "set_alarm":
        set_alarm(entities.get("time", "07:00"))
        speak("Будильник установлен", emotion="excited", voice="elena")
    elif intent == "cancel_alarm":
        cancel_alarm()
        speak("Будильник отменён", emotion="sad", voice="pavel")