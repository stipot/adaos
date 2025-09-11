import json
from pathlib import Path
from adaos.services.agent_context import get_ctx


def _env_path() -> Path:
    return get_ctx().paths.skills_dir() / ".skill_env.json"


def get_env(key: str, default=None):
    env_file = _env_path()
    if env_file.exists():
        data = json.loads(env_file.read_text(encoding="utf-8"))
        return data.get(key, default)
    return default


def set_env(key: str, value):
    env_file = _env_path()
    if env_file.exists():
        data = json.loads(env_file.read_text(encoding="utf-8"))
    else:
        data = {}
    data[key] = value
    env_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
