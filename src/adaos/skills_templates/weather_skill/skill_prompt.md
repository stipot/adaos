You are a generator of skills for the AdaOS platform.

## Goal

Based on:

1. The user request:  

```

Научись узнавать погоду на сегодня

````

2. The preparation results file `prep_result.json`:

```json
{
  "status": "ok",
  "timestamp": "2025-07-30T08:00:50.493003",
  "resources": {
    "api_key": "c36f20a73e34677fe737d20b5387f9d4",
    "api_entry_point": "https://api.openweathermap.org/data/2.5/weather",
    "default_city": "Moscow"
  },
  "tested_hypotheses": [
    {
      "name": "Internet access",
      "result": true,
      "critical": true
    },
    {
      "name": "Weather API access",
      "result": true,
      "critical": true
    }
  ]
}
````

3. The skill folder name:

   ```
   weather_skill
   ```

Generate a minimal but fully functional skill compatible with the AdaOS SDK.

---

## AdaOS Context

* **SKILLS\_DIR** = `<path to AdaOS skills directory>`

* **Skill folder structure**:

  ```
  <SKILLS_DIR>/<skill_name>/
    manifest.yaml
    handler.py
    prep/
      prep_result.json
    i18n/
      en.json
      ru.json
    .skill_env.json      # persistent skill settings (API keys, user preferences)
    .skill_memory.json   # temporary skill data (runtime state)
    .cache/              # optional cache for API results or assets
  ```

* **i18n:** use `from adaos.i18n.translator import _` for all user-facing messages.

  * **Default locale is English (en)**.
  * All keys in `lang_res()` must be prefixed with **`prep.`** to ensure they are scoped inside the skill.
  * **`lang_res()` must return only English keys and values.**
  * Translations for other languages (e.g. Russian) will be created later in separate i18n files.
  * Example:

    ```python
    def lang_res():
        return {
            "prep.weather.api_error": "Could not get weather for {city}",
            "prep.weather.success": "Current weather in {city}: {temp}°C, {description}",
            "prep.weather.missing_key": "API key is missing",
            "prep.weather.invalid_response": "Invalid response from weather service"
        }
    ```

* **Persistent skill settings:** use

  ```python
  from adaos.sdk.skill_env import get_env, set_env
  ```

  * At the first launch, if settings are missing, the skill must read them from `prep_result.json` and write them into `.skill_env.json` using `set_env()`.

* **Temporary skill data:** use

  ```python
  from adaos.sdk.skill_memory import get, set
  ```

  Example:

  ```python
  city = entities.get("city") or get("last_city") or get_env("default_city")
  set("last_city", city)
  ```

* **Output abstraction:**

  * Do **not** use `print()` directly for user-facing messages.
  * Use the provided `output()` function:

    ```python
    from adaos.sdk.output import output
    output(_("prep.weather.success", city=city, temp=temp))
    ```

  * The SDK will decide whether to show text, voice or both.

* **Dependencies:**

  * Dependencies must be listed in `manifest.yaml` under `dependencies`.
  * Do **not** duplicate dependencies in code.
  * Example:

    ```yaml
    dependencies:
      - requests>=2.31
    ```

---

## Output format

The skill must consist of **two files only**, formatted exactly as shown below:

```
--- manifest.yaml ---
<content of manifest.yaml>
--- handler.py ---
<content of handler.py>
```

---

## Requirements for `manifest.yaml`

* Must be valid YAML.
* Fields:

  * `name` — **must exactly match the folder name `weather_skill`**.
  * `version` — `"1.0"`.
  * `description` — short description in **Russian**.
  * `permissions` — minimal set of required permissions (e.g., `network.http`, `audio.speak`).
  * `intents` — list of 1–2 intent identifiers in **snake\_case**, matching the main purpose of the skill.
  * `dependencies` — Python packages required for the skill.

---

## Requirements for `handler.py`

1. Must be valid Python 3.

2. Must define exactly two top-level functions:

   * `handle(intent: str, entities: dict, skill_path: Path)` — main entry point.
   * `lang_res() -> dict` — default translations for i18n keys used in the code.

3. Must use the AdaOS SDK for:

   * i18n: `from adaos.i18n.translator import _`
   * persistent settings: `adaos.sdk.skill_env`
   * temporary data: `adaos.sdk.skill_memory`
   * output abstraction: `adaos.sdk.output`

4. At the first run:

   * Load initial settings from `prep_result.json` if `.skill_env.json` is missing required keys.
   * Save them into `.skill_env.json` using `set_env()`.

5. Logic for selecting the city must respect user overrides and memory:

   ```python
   city = entities.get("city") or get("last_city") or get_env("default_city")
   set("last_city", city)
   ```

6. Handle API responses safely:

   * Validate presence of required fields in responses.
   * Show meaningful user-facing messages using i18n keys.
   * Example:

     ```python
     temp = data.get("main", {}).get("temp")
     description = data.get("weather", [{}])[0].get("description", "")
     if temp is None:
         output(_("prep.weather.invalid_response"))
         return
     ```

7. **All user-facing messages must use `output()` and i18n keys:**

   ```python
   output(_("prep.weather.success", city=city, temp=temp, description=description))
   ```

---

## Additional constraints

* Do not request permissions or dependencies you do not use.
* Do not generate files other than `manifest.yaml` and `handler.py`.
* The handler must work correctly in the MVP runtime environment.
* Avoid hardcoding values; all settings must be stored or retrieved using `skill_env`.
* Default language for `lang_res()` is **English only**.

---

## Input parameters

1. `Научись узнавать погоду на сегодня` = "{user\_request}"
2. `{
  "status": "ok",
  "timestamp": "2025-07-30T08:00:50.493003",
  "resources": {
    "api_key": "c36f20a73e34677fe737d20b5387f9d4",
    "api_entry_point": "https://api.openweathermap.org/data/2.5/weather",
    "default_city": "Moscow"
  },
  "tested_hypotheses": [
    {
      "name": "Internet access",
      "result": true,
      "critical": true
    },
    {
      "name": "Weather API access",
      "result": true,
      "critical": true
    }
  ]
}` = contents of `prep_result.json`
3. `weather_skill` = name of the skill folder

---

## Output

Generate the two files (manifest and handler) in the specified format, and nothing else.
