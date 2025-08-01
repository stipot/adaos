import json
import logging
from datetime import datetime
from pathlib import Path
import requests
from adaos.i18n.translator import _


def lang_res():
    return {
        "prep.ask_default_city": "Enter default city for weather forecast: ",
        "prep.ask_api_key": "Enter API key for OpenWeatherMap: ",
        "prep.ask_api_entry_point": "Enter API entry point: ",
        "prep.test_internet_access": "Testing internet access...",
        "prep.fail_internet": "Internet access failed",
        "prep.test_weather_api": "Testing weather API...",
        "prep.fail_weather_api": "Weather API access failed",
        "prep.unexpected_error": "Unexpected error occurred",
        "prep.summary_header": "Preparation Summary",
        "prep.collected_resources": "Collected Resources",
        "prep.tested_hypotheses": "Tested Hypotheses",
        "prep.failed_header": "Preparation Failed",
        "prep.reason": "Reason",
    }


def test_internet_access():
    try:
        response = requests.get("https://www.google.com", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def test_weather_api(api_key, api_entry_point, city):
    try:
        params = {"q": city, "appid": api_key, "units": "metric"}
        response = requests.get(api_entry_point, params=params, timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def run_prep(skill_path: Path):
    prep_result = {"status": "ok", "timestamp": datetime.utcnow().isoformat(), "resources": {}, "tested_hypotheses": []}

    # Setup logging
    logs_dir = skill_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    logging.basicConfig(filename=logs_dir / "prep.log", level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    try:
        # Test internet access
        logging.info(_("prep.test_internet_access"))
        internet_ok = test_internet_access()
        prep_result["tested_hypotheses"].append({"name": "Internet access", "result": internet_ok, "critical": True})
        if not internet_ok:
            prep_result["status"] = "failed"
            prep_result["reason"] = _("prep.fail_internet")
            return prep_result

        # Collect API key
        api_key = input(_("prep.ask_api_key"))
        prep_result["resources"]["api_key"] = api_key

        # Set default API entry point
        api_entry_point = "https://api.openweathermap.org/data/2.5/weather"
        prep_result["resources"]["api_entry_point"] = api_entry_point

        # Collect default city
        default_city = input(_("prep.ask_default_city"))
        prep_result["resources"]["default_city"] = default_city

        # Test weather API
        logging.info(_("prep.test_weather_api"))
        weather_api_ok = test_weather_api(api_key, api_entry_point, default_city)
        prep_result["tested_hypotheses"].append({"name": "Weather API access", "result": weather_api_ok, "critical": True})
        if not weather_api_ok:
            prep_result["status"] = "failed"
            prep_result["reason"] = _("prep.fail_weather_api")
            return prep_result

    except Exception as e:
        logging.error(_("prep.unexpected_error"), exc_info=True)
        prep_result["status"] = "failed"
        prep_result["reason"] = _("prep.unexpected_error")
        return prep_result

    # Write output files with UTF-8 encoding
    with open(skill_path / "prep_result.json", "w", encoding="utf-8") as f:
        json.dump(prep_result, f, indent=2)

    with open(skill_path / "prep_result_prompt.md", "w", encoding="utf-8") as f:
        if prep_result["status"] == "ok":
            f.write(f"# {_('prep.summary_header')}\n\n")
            f.write(f"## {_('prep.collected_resources')}\n")
            for key, value in prep_result["resources"].items():
                f.write(f"- **{key}**: {value}\n")
            f.write(f"\n## {_('prep.tested_hypotheses')}\n")
            for hypothesis in prep_result["tested_hypotheses"]:
                status = "✅" if hypothesis["result"] else "❌"
                f.write(f"- {status} {hypothesis['name']}\n")
        else:
            f.write(f"# {_('prep.failed_header')}\n\n")
            f.write(f"**{_('prep.reason')}**: {prep_result['reason']}\n")

    return prep_result
