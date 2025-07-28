import json
import os
from datetime import datetime, timedelta
import threading
import time
from runtime.sdk.audio import speak

CONFIG_PATH = os.path.join(os.path.dirname(__file__), '../config.json')
RESPONSES_PATH = os.path.join(os.path.dirname(__file__), '../assets/responses/')

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f)

def set_alarm(time_str):
    alarm_time = datetime.strptime(time_str, "%H:%M").time()
    now = datetime.now()
    alarm_dt = datetime.combine(now.date(), alarm_time)
    if alarm_dt < now:
        alarm_dt += timedelta(days=1)

    cfg = {"alarm": alarm_dt.isoformat()}
    save_config(cfg)
    speak("Будильник установлен", emotion="happy")

    def wait_and_ring():
        time.sleep((alarm_dt - datetime.now()).total_seconds())
        print("[ALARM] Время вставать!")  # отправка в аудио-плеер

    threading.Thread(target=wait_and_ring).start()

def cancel_alarm():
    save_config({})
    speak("Будильник отменён", emotion="sad")

def handle(intent, entities):
    if intent == "set_alarm":
        time_str = entities.get("time", "07:00")
        set_alarm(time_str)
    elif intent == "cancel":
        cancel_alarm()
