# src\adaos\api\tool_bridge.py
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field
import importlib.util
from typing import Any, Dict

from adaos.apps.api.auth import require_token
from adaos.sdk.decorators import resolve_tool
from adaos.services.observe import attach_http_trace_headers
from adaos.services.agent_context import get_ctx, AgentContext


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
    model_config = {"extra": "ignore"}


@router.post("/tools/call", dependencies=[Depends(require_token)])
async def call_tool(body: ToolCall, request: Request, response: Response, ctx: AgentContext = Depends(get_ctx)):
    # 1) Разбираем "<skill_name>:<public_tool_name>"
    if ":" not in body.tool:
        raise HTTPException(status_code=400, detail="tool must be in '<skill_name>:<public_tool_name>' format")

    skill_name, public_tool = body.tool.split(":", 1)
    if not skill_name or not public_tool:
        raise HTTPException(status_code=400, detail="invalid tool spec")

    # 2) Устанавливаем текущий навык на время выполнения запроса
    if not ctx.skill_ctx.set(skill_name, ctx.paths.skills_dir() / skill_name):  # set_current_skill(skill_name):
        raise HTTPException(status_code=503, detail=f"The skill {skill_name} is not found")

    # 3) Получаем текущий навык (после установки)
    current = ctx.skill_ctx.get()  # get_current_skill
    if current is None or current.path is None or current.name is None:
        raise HTTPException(status_code=503, detail="current skill is not set")

    # 4) Проверяем соответствие имени
    if current.name != skill_name:
        # это защита от сторонних перезаписей контекста
        raise HTTPException(status_code=404, detail=f"skill '{skill_name}' is not the current skill (current: '{current.name}')")

    # 5) Грузим модуль обработчика навыка
    handler_file = current.path / "handlers" / "main.py"
    if not handler_file.exists():
        raise HTTPException(status_code=404, detail=f"skill '{skill_name}' is not installed (handlers/main.py missing)")

    mod_name = f"adaos_skill_{skill_name}_handlers_main"
    spec = importlib.util.spec_from_file_location(mod_name, handler_file)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail="failed to load skill module")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to import skill '{skill_name}': {type(e).__name__}: {e}")

    # 6) Ищем инструмент по публичному имени, зарегистрированному @tool("<public_name>")
    fn = resolve_tool(mod_name, public_tool)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"tool '{public_tool}' is not exported by skill '{skill_name}'")

    # 7) Вызываем инструмент (sync/async совместимость)
    # trace_id в HTTP: читаем входной/ставим в ответ
    trace = attach_http_trace_headers(request.headers, response.headers)
    args = body.arguments or {}
    try:
        result = fn(**args)
        if hasattr(result, "__await__"):
            result = await result
    except TypeError as e:
        # частый кейс: некорректные аргументы
        raise HTTPException(status_code=400, detail=f"invalid arguments: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"tool runtime error: {type(e).__name__}: {e}")

    return {"ok": True, "result": result, "trace_id": trace}
