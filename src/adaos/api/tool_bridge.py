from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import importlib.util
from typing import Any, Dict

from adaos.api.auth import require_token
from adaos.sdk.context import get_current_skill, set_current_skill
from adaos.sdk.decorators import resolve_tool


router = APIRouter()


class ToolCall(BaseModel):
    """
    Вызов инструмента навыка:
      tool: "<skill_name>:<public_tool_name>"
      arguments: {...}  # опционально
      context:   {...}  # опционально (резерв на будущее)
    """

    tool: str
    arguments: Dict[str, Any] | None = None
    context: Dict[str, Any] | None = None


@router.post("/tools/call", dependencies=[Depends(require_token)])
async def call_tool(body: ToolCall):
    # 1) Разбираем "<skill_name>:<public_tool_name>"
    if ":" not in body.tool:
        raise HTTPException(status_code=400, detail="tool must be in '<skill_name>:<public_tool_name>' format")

    skill_name, public_tool = body.tool.split(":", 1)
    if not skill_name or not public_tool:
        raise HTTPException(status_code=400, detail="invalid tool spec")

    # 2) Устанавливаем текущий навык на время выполнения запроса
    set_current_skill(skill_name)

    # 3) Получаем текущий навык (после установки)
    current = get_current_skill()
    if current is None or current.path is None or current.name is None:
        raise HTTPException(status_code=503, detail="current skill is not set")

    # 4) Проверяем соответствие имени
    if current.name != skill_name:
        # это защита от сторонних перезаписей контекста
        raise HTTPException(status_code=404, detail=f"skill '{skill_name}' is not the current skill (current: '{current.name}')")

    # 5) Грузим модуль обработчика навыка
    handler_file = current.path / "handlers" / "main.py"
    if not handler_file.exists():
        raise HTTPException(status_code=404, detail=f"handler not found for skill '{skill_name}'")

    mod_name = f"adaos_skill_{skill_name}_handlers_main"
    spec = importlib.util.spec_from_file_location(mod_name, handler_file)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail="failed to load skill module")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 6) Ищем инструмент по публичному имени, зарегистрированному @tool("<public_name>")
    fn = resolve_tool(mod_name, public_tool)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"tool '{public_tool}' is not exported by skill '{skill_name}'")

    # 7) Вызываем инструмент (sync/async совместимость)
    args = body.arguments or {}
    result = fn(**args)
    if hasattr(result, "__await__"):
        result = await result

    return {"ok": True, "result": result}
