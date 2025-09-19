import os
import re
import yaml
from pathlib import Path

from adaos.services.agent_context import get_ctx


def process_llm_output(llm_output: str, skill_name_hint="Skill"):
    """
    Обрабатывает текст от LLM и создаёт структуру навыка.
    """
    # Разделяем файлы
    manifest_match = re.search(r"--- manifest.yaml ---\n(.*?)--- handler.py ---", llm_output, re.S)
    handler_match = re.search(r"--- handler.py ---\n(.*)", llm_output, re.S)

    if not manifest_match or not handler_match:
        raise ValueError("Не удалось распарсить ответ LLM. Проверь формат.")

    manifest_text = manifest_match.group(1).strip()
    handler_text = handler_match.group(1).strip()

    # Парсим manifest.yaml
    manifest = yaml.safe_load(manifest_text)
    skill_name = manifest.get("name", skill_name_hint)

    # Проверяем права
    permissions = manifest.get("permissions", [])
    allowed_permissions = {"audio.speak", "alarm.set", "alarm.cancel"}
    for p in permissions:
        if p not in allowed_permissions:
            raise ValueError(f"Запрещённое право: {p}")

    # Проверяем интенты
    intents = manifest.get("intents", [])
    if not intents:
        raise ValueError("Нет интентов в манифесте.")

    # Создаём папку навыка
    ctx = get_ctx()
    skills_dir_attr = getattr(ctx.paths, "skills_dir", None)
    skills_root = skills_dir_attr() if callable(skills_dir_attr) else skills_dir_attr
    if not skills_root:
        raise RuntimeError("skills directory path is not configured in agent context")
    skill_dir = Path(skills_root) / skill_name.lower()
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Записываем файлы
    (skill_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
    (skill_dir / "handler.py").write_text(handler_text, encoding="utf-8")

    print(f"[OK] Навык {skill_name} установлен.")
    return skill_dir
