import os
from openai import OpenAI


def getOpenAIClient():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_test_yaml(user_request: str):
    # Пример использования LLM
    prompt = f"Сформируй YAML тест для запроса: {user_request}"
    response = getOpenAIClient().chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
    return response.choices[0].message["content"]


def generate_test_yaml(user_request: str) -> str:
    """
    Генерация тестового сценария в YAML на основе запроса пользователя.
    """
    return f"""
name: test_alarm
description: Проверка навыка из запроса: {user_request}
steps:
  - type: voice_input
    phrase: "поставь будильник на 7 утра"
  - type: expect_response
    contains: "будильник установлен"
  - type: check_state
    resource: "alarms"
    condition: "exists(time='07:00')"
"""


def generate_skill(user_request: str) -> str:
    """
    Генерация навыка (manifest.yaml + handler.py) в ответ на запрос пользователя.
    """
    return """
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
"""
