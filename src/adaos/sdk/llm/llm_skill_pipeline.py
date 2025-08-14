from pathlib import Path
from adaos.agent.core.test_runner import TestRunner
from .llm_client import generate_test_yaml, generate_skill
from process_llm_output import process_llm_output


def skill_creation_pipeline(user_request: str):
    """
    Полный цикл создания навыка через LLM и тестирование.
    """
    print(f"[PIPELINE] Запрос пользователя: {user_request}")

    # 1. Генерация теста
    test_yaml = generate_test_yaml(user_request)
    test_path = Path(f"runtime/tests/test_{hash(user_request)}.yaml")
    test_path.write_text(test_yaml, encoding="utf-8")

    # 2. Запуск теста без навыка
    runner = TestRunner()
    if runner.run_test(str(test_path)):
        print("[PIPELINE] Навык уже существует, тест пройден.")
        return {"status": "exists", "logs": runner.logs}

    print("[PIPELINE] Тест провален, пробуем создать новый навык...")

    # 3. Генерация навыка
    skill_code = generate_skill(user_request)
    try:
        skill_dir = process_llm_output(skill_code)
    except Exception as e:
        return {"status": "fail", "logs": [f"[ERROR] Ошибка установки навыка: {e}"]}

    # 4. Запуск теста снова
    runner = TestRunner()
    if runner.run_test(str(test_path)):
        print(f"[PIPELINE] Навык успешно создан: {skill_dir}")
        return {"status": "success", "logs": runner.logs}
    else:
        print(f"[PIPELINE] Не удалось создать навык: {skill_dir}")
        return {"status": "fail", "logs": runner.logs}
