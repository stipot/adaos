# src/adaos/sdk/skill_api.py
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class SkillContext:
    skill_path: Path
    locale: str = "ru"
    # можно добавить logger, output, tts и т.д. по мере надобности


@dataclass
class SkillResult:
    text: str
    data: Optional[Dict[str, Any]] = None


def run_skill(handle_fn, intent: str, entities: Dict[str, Any], ctx: SkillContext) -> SkillResult:
    """
    Унифицирует возврат значения от разных реализаций handle.
    Если текущий handle ничего не возвращает — адаптируйте его позже.
    """
    out = handle_fn(intent=intent, entities=entities, skill_path=ctx.skill_path)
    if isinstance(out, SkillResult):
        return out
    if isinstance(out, dict):
        return SkillResult(text=out.get("text") or out.get("speech") or "", data=out)
    if isinstance(out, str):
        return SkillResult(text=out)
    # fallback — пустой результат
    return SkillResult(text="")
