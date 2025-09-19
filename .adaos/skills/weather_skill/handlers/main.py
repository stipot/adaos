import json
from typing import Dict, Optional, Tuple

import requests

from adaos.sdk.skills.i18n import _
from adaos.sdk.data.context import get_current_skill
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.bus import emit
from adaos.sdk.data.skill_memory import get as get_env, set as set_env
from adaos.services.agent_context import get_ctx

"""   ```

  Example:

  ```python
  city = entities.get("city") or get("last_city") or get_env("default_city") """
from adaos.sdk.data.skill_memory import get, set


# ---------------------------
# helpers: config & api calls
# ---------------------------


def output(txt: str):
    print(txt)


def _load_and_cache_config() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Возвращает (api_key, api_entry_point, default_city), беря сперва из env,
    затем — из prep/prep_result.json, и кэширует обратно в env.
    """
    api_key = get_env("api_key")
    api_entry_point = get_env("api_entry_point")
    default_city = get_env("default_city")

    if api_key and api_entry_point and default_city:
        return api_key, api_entry_point, default_city

    # попытка подтянуть из prep_result.json
    try:
        prep_file = get_current_skill().path / "prep" / "prep_result.json"
        if prep_file.exists():
            data = json.loads(prep_file.read_text(encoding="utf-8"))
            res = data.get("resources", {}) or {}
            api_key = api_key or res.get("api_key")
            api_entry_point = api_entry_point or res.get("api_entry_point")
            default_city = default_city or res.get("default_city")

            if api_key:
                set_env("api_key", api_key)
            if api_entry_point:
                set_env("api_entry_point", api_entry_point)
            if default_city:
                set_env("default_city", default_city)
    except Exception:
        # молча: отсутствие prep — нормальный кейс
        pass

    return api_key, api_entry_point, default_city


def _resolve_city(payload_city: Optional[str] = None) -> Optional[str]:
    """
    Выбираем город по приоритету:
    1) переданный в payload/entities,
    2) last_city (skill_memory),
    3) default_city (env).
    """
    city = payload_city or get("last_city") or get_env("default_city")
    if city:
        set("last_city", city)
    return city


def _fetch_weather(api_entry_point: str, api_key: str, city: str) -> Tuple[bool, Dict]:
    """
    Делает запрос к погодному API. Возвращает (ok, data_or_error).
    """
    try:
        r = requests.get(
            api_entry_point,
            params={"q": city, "appid": api_key, "units": "metric", "lang": "en"},
            timeout=6,
        )
    except Exception as e:
        return False, {"error": f"request_error: {e!s}"}

    if r.status_code != 200:
        return False, {"error": f"api_status_{r.status_code}"}

    try:
        d = r.json()
    except Exception:
        return False, {"error": "invalid_json"}

    temp = (d.get("main") or {}).get("temp")
    desc = (d.get("weather") or [{}])[0].get("description", "")

    if temp is None:
        return False, {"error": "invalid_response"}

    return True, {"city": city, "temp": temp, "description": desc}


# ---------------------------
# primary entrypoints
# ---------------------------


def handle(topic: str, payload: dict):
    """
    Унифицированная точка входа для локального запуска навыка:
    `adaos skill run ... --topic nlp.intent.weather.get --payload '{"city": "Berlin"}'`

    Ожидает:
      - topic: строка события/интента
      - payload: словарь с возможным ключом "city"
    """
    get_ctx().skill_ctx.set("weather_skill", get_ctx().paths.skills_dir() / "weather_skill")
    api_key, api_entry_point, default_city = _load_and_cache_config()
    if not api_key:
        output(_("prep.weather.missing_key"))
        return

    city = _resolve_city((payload or {}).get("city"))
    if not city:
        # нет города — сообщим и выйдем
        output(_("prep.weather.api_error", city=""))
        return

    ok, data = _fetch_weather(api_entry_point, api_key, city)
    if not ok:
        output(_("prep.weather.api_error", city=city))
        return

    output(_("prep.weather.success", city=data["city"], temp=data["temp"], description=data["description"]))


# Back-compat: если кто-то всё ещё вызывает старую сигнатуру
def handle_intent(intent: str, entities: dict):
    """
    Совместимость со старым вызовом `handle(intent, entities)`.
    Преобразуем к новому виду.
    """
    city = (entities or {}).get("city")
    return handle(intent or "nlp.intent.weather.get", {"city": city} if city else {})


# инструмент для LLM (/api/tools/call)
@tool("get_weather")
def get_weather(city: str) -> dict:
    api_key, api_entry_point, default_city = _load_and_cache_config()

    if not api_key or not api_entry_point:
        return {"ok": False, "error": "missing api config"}

    city = city or default_city or get("last_city")
    if not city:
        return {"ok": False, "error": "missing city"}

    ok, data = _fetch_weather(api_entry_point, api_key, city)
    if not ok:
        return {"ok": False, **data}

    return {"ok": True, **data}


# подписка на событие: nlp.intent.weather.get
@subscribe("nlp.intent.weather.get")
async def on_weather_intent(evt):
    api_key, api_entry_point, _ = _load_and_cache_config()
    if not api_key or not api_entry_point:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.missing_key")},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    city = _resolve_city((evt.payload or {}).get("city"))
    if not city:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.api_error", city="")},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    ok, data = _fetch_weather(api_entry_point, api_key, city)
    if ok:
        msg = _("prep.weather.success", city=data["city"], temp=data["temp"], description=data["description"])
        await emit("ui.notify", {"text": msg}, actor=evt.actor, source="weather_skill", trace_id=evt.trace_id)
    else:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.api_error", city=city)},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )


def lang_res() -> Dict[str, str]:
    return {
        "prep.weather.api_error": "Could not get weather for {city}",
        "prep.weather.success": "Current weather in {city}: {temp}°C, {description}",
        "prep.weather.missing_key": "API key is missing",
        "prep.weather.invalid_response": "Invalid response from weather service",
    }
