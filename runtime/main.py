from skill_loader import SkillLoader
from sdk.a_permissions import set_current_permissions
from sdk.voice.asr import VoiceRecognizer
from sdk.voice.wake_word import WakeWordVosk


def parse_intent(raw):
    if "будильник" in raw and "отмени" in raw:
        return "cancel_alarm", {}
    elif "будильник" in raw:
        return "set_alarm", {"time": raw.split()[-1]}
    return None, None


def main():
    loader = SkillLoader()
    loader.load_skills()
    asr = VoiceRecognizer()
    detector = WakeWordVosk(wake_word="ада")

    print("AdaOS Runtime запущен. Ожидание команд...")

    while True:
        if detector.wait_for_wake_word():
            print("[INFO] Активация ассистента")
            raw = asr.listen()
            if raw in ["выход", "завершить"]:  # ["exit", "quit"]
                break
            intent, entities = parse_intent(raw)
            if intent:
                skill_data = loader.get_skill_for_intent(intent)
                if skill_data:
                    # Устанавливаем права текущего навыка
                    set_current_permissions(skill_data["permissions"])
                    try:
                        skill_data["handler"].handle(intent, entities)
                    except PermissionError as e:
                        print(f"[SECURITY] {e}")
                else:
                    print("[INFO] Навык не найден.")
            else:
                print("[INFO] Неизвестная команда.")


if __name__ == "__main__":
    main()
