from .a_permissions import require_permission
from datetime import datetime, timedelta
import threading
import time

alarms = {}

def set_alarm(time_str):
    require_permission("alarm.set")
    now = datetime.now()
    target = datetime.strptime(time_str, "%H:%M").time()
    alarm_time = datetime.combine(now.date(), target)
    if alarm_time < now:
        alarm_time += timedelta(days=1)

    delay = (alarm_time - datetime.now()).total_seconds()

    def ring():
        print("[ALARM] Сработал будильник!")
    t = threading.Timer(delay, ring)
    t.start()

    alarms["current"] = t
    print(f"[Scheduler] Будильник установлен на {alarm_time}")

def cancel_alarm():
    require_permission("alarm.cancel")
    if "current" in alarms:
        alarms["current"].cancel()
        print("[Scheduler] Будильник отменён")
