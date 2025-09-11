import json
from pathlib import Path
from adaos.apps.bootstrap import get_ctx


def _memory_path() -> Path:
    return get_ctx().skill_ctx.get().path / ".skill_memory.json"


def get(key: str, default=None):
    mem_file = _memory_path()
    if mem_file.exists():
        data = json.loads(mem_file.read_text(encoding="utf-8"))
        return data.get(key, default)
    return default


def set(key: str, value):
    mem_file = _memory_path()
    if mem_file.exists():
        data = json.loads(mem_file.read_text(encoding="utf-8"))
    else:
        data = {}
    data[key] = value
    mem_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
