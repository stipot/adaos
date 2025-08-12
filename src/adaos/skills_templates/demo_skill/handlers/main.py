import json
from pathlib import Path
from typing import Dict
import requests

from adaos.sdk.skills.i18n import _
from adaos.sdk.context import current_skill_path
from adaos.sdk.output import output
from adaos.sdk.skill_env import get_env, set_env
from adaos.sdk.skill_memory import get, set


def handle(intent: str, entities: dict):

    # Ensure environment settings are loaded
    api_key = get_env("api_key")
    api_entry_point = get_env("api_entry_point")
    default_city = get_env("default_city")

    if not api_key or not api_entry_point or not default_city:
        prep_file = current_skill_path / "prep" / "prep_result.json"
        if prep_file.exists():
            with open(prep_file, "r", encoding="utf-8") as f:
                prep_data = json.load(f)
                resources = prep_data.get("resources", {})
                api_key = resources.get("api_key")
                api_entry_point = resources.get("api_entry_point")
                default_city = resources.get("default_city")
                if api_key:
                    set_env("api_key", api_key)
                if api_entry_point:
                    set_env("api_entry_point", api_entry_point)
                if default_city:
                    set_env("default_city", default_city)

    if not api_key:
        output(_("prep.weather.missing_key"))
        return

    # Determine the city
    city = entities.get("city") or get("last_city") or get_env("default_city")
    set("last_city", city)

    # Make the API request
    try:
        response = requests.get(
            api_entry_point,
            params={"q": city, "appid": api_key, "units": "metric", "lang": "en"},
            timeout=5,
        )
        if response.status_code != 200:
            output(_("prep.weather.api_error", city=city))
            return

        data = response.json()
        temp = data.get("main", {}).get("temp")
        description = data.get("weather", [{}])[0].get("description", "")

        if temp is None:
            output(_("prep.weather.invalid_response"))
            return

        output(_("prep.weather.success", city=city, temp=temp, description=description))
    except Exception:
        output(_("prep.weather.api_error", city=city))


def lang_res() -> Dict[str, str]:
    return {
        "prep.weather.api_error": "Could not get weather for {city}",
        "prep.weather.success": "Current weather in {city}: {temp}°C, {description}",
        "prep.weather.missing_key": "API key is missing",
        "prep.weather.invalid_response": "Invalid response from weather service",
    }
