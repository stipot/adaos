# src\adaos\sdk\skill_validator.py
from __future__ import annotations
import os, sys, json, subprocess, importlib.util
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
import copy

import yaml
from jsonschema import validate as js_validate, Draft202012Validator, ValidationError, Draft7Validator

from adaos.sdk.context import get_current_skill, set_current_skill
from adaos.sdk.decorators import resolve_tool, _SUBSCRIPTIONS  # реестр подписок из декораторов
from adaos.sdk.skill_env import get_env
from adaos.sdk.skill_memory import get as mem_get  # пригодится позже
from adaos.services.agent_context import AgentContext
from adaos.apps.bootstrap import get_ctx

SCHEMA_PATH = Path(__file__).with_name("skill_schema.json")


@dataclass
class Issue:
    level: str  # "error" | "warning"
    code: str
    message: str
    where: str | None = None


@dataclass
class ValidationReport:
    ok: bool
    issues: List[Issue]


def _load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise RuntimeError(f"failed to read yaml: {e}")


def _normalize_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Подставляем дефолты для опциональных полей и приводим типы:
    - dependencies         → []
    - events               → {}
      - events.subscribe   → []
      - events.publish     → []
    - tools                → []
    - exports              → {}
      - exports.tools      → []
    """
    s = copy.deepcopy(spec or {})
    # простые поля
    if s.get("description") is None:
        s["description"] = ""
    # массивы/объекты
    if not isinstance(s.get("dependencies"), list):
        s["dependencies"] = []
    if not isinstance(s.get("tools"), list):
        s["tools"] = []
    if not isinstance(s.get("exports"), dict):
        s["exports"] = {}
    if not isinstance(s["exports"].get("tools"), list):
        s["exports"]["tools"] = []
    # events
    if not isinstance(s.get("events"), dict):
        s["events"] = {}
    if not isinstance(s["events"].get("subscribe"), list):
        s["events"]["subscribe"] = []
    if not isinstance(s["events"].get("publish"), list):
        s["events"]["publish"] = []
    return s


def _static_checks(skill_dir: Path, install_mode: bool) -> List[Issue]:
    issues: List[Issue] = []
    # 1) skill.yaml
    sy = skill_dir / "skill.yaml"
    if not sy.exists():
        issues.append(Issue("error", "missing.skill_yaml", "skill.yaml not found", str(sy)))
        return issues
    raw = _read_yaml(sy)
    data = _normalize_spec(raw)
    # 2) схема
    try:
        schema = _load_schema()
        Draft202012Validator(schema).validate(data)
    except ValidationError as e:
        issues.append(Issue("error", "schema.invalid", f"skill.yaml schema violation: {e.message}", "skill.yaml"))
        return issues
    # 3) обязательные файлы
    handler = skill_dir / "handlers" / "main.py"
    if not handler.exists():
        issues.append(Issue("error", "missing.handler", "handlers/main.py not found", str(handler)))
    # 4) инструменты: уникальность имён
    tools = data.get("tools") or []
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    if len(names) != len(set(names)):
        issues.append(Issue("error", "tools.duplicate_names", "duplicate tool names in skill.yaml", "tools[]"))
    # 5) схемы инструментов — базовая валидация структуры (что это объекты)
    for t in tools:
        if not isinstance(t, dict):
            issues.append(Issue("error", "tools.invalid_item", "tool item must be an object", "tools[]"))
            continue
        if not isinstance(t.get("input_schema"), dict):
            issues.append(Issue("error", "tools.input_schema.invalid", f"tool '{t.get('name')}' input_schema must be object", "tools[].input_schema"))
        if t.get("output_schema") is not None and not isinstance(t.get("output_schema"), dict):
            issues.append(
                Issue("warning" if not install_mode else "error", "tools.output_schema.invalid", f"tool '{t.get('name')}' output_schema should be object", "tools[].output_schema")
            )
    # 6) events — строки
    ev = data.get("events") or {}
    for key in ("subscribe", "publish"):
        arr = ev.get(key) or []
        for i, v in enumerate(arr):
            if not isinstance(v, str) or not v.strip():
                issues.append(Issue("error", f"events.{key}.invalid", f"events.{key}[{i}] must be non-empty string", f"events.{key}[{i}]"))
    return issues


def _dynamic_checks(skill_name: str, skill_dir: Path, install_mode: bool, probe_tools: bool) -> List[Issue]:
    """
    Импортируем handlers/main.py в ОТДЕЛЬНОМ процессе Python,
    чтобы исключить побочные эффекты. Используем resolve_tool через модульное имя.
    """
    code = f"""
import os, json, importlib.util
os.environ['ADAOS_VALIDATE'] = '1'
mod_name = 'adaos_skill_{skill_name}_handlers_main'
handler_file = r'{(skill_dir / 'handlers' / 'main.py').as_posix()}'
spec = importlib.util.spec_from_file_location(mod_name, handler_file)
if spec is None or spec.loader is None:
    print(json.dumps({{"ok": False, "error": "spec/load failure"}}))
    raise SystemExit(0)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# introspect exported tools and subscriptions
try:
    from adaos.sdk.decorators import _TOOLS, _SUBSCRIPTIONS
except Exception:
    _TOOLS, _SUBSCRIPTIONS = {{}}, []

exports = list((_TOOLS.get(mod_name) or {{}}).keys())
subs = [t for (t, _fn) in _SUBSCRIPTIONS]
print(json.dumps({{"ok": True, "tools": exports, "subs": subs}}))
"""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    issues: List[Issue] = []
    if proc.returncode != 0:
        issues.append(Issue("error", "import.failed", f"handler import failed: {proc.stderr.strip() or proc.stdout.strip()}"))
        return issues
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        issues.append(Issue("error", "introspect.invalid_json", "introspection did not return valid JSON"))
        return issues
    if not payload.get("ok"):
        issues.append(Issue("error", "introspect.failed", str(payload)))
        return issues

    # Сверка с skill.yaml
    data = _normalize_spec(_read_yaml(skill_dir / "skill.yaml"))
    declared_tools = [t.get("name") for t in (data.get("tools") or []) if isinstance(t, dict)]
    exported_tools = set(payload.get("tools") or [])
    for name in declared_tools:
        if name not in exported_tools:
            issues.append(Issue("error", "tools.missing_export", f"tool '{name}' declared in skill.yaml but not exported by @tool", "tools[].name"))
    declared_subs = set((data.get("events") or {}).get("subscribe") or [])
    exported_subs = set(payload.get("subs") or [])
    for topic in declared_subs:
        if topic not in exported_subs:
            issues.append(Issue("error", "events.missing_sub", f"no @subscribe handler for '{topic}'", "events.subscribe[]"))

    # (опционально) пробный вызов инструментов — здесь пропускаем, чтобы не исполнять код навыка
    return issues


@dataclass(slots=True)
class SkillValidationService:
    ctx: AgentContext

    def validate(self, skill_name: Optional[str] = None, *, strict: bool = False, install_mode: Optional[bool] = False, probe_tools: bool = False) -> ValidationReport:
        ctx = get_ctx()
        # определяем каталог навыка
        if skill_name:
            if not set_current_skill(skill_name):
                return ValidationReport(False, [Issue("error", "skill.context.missing", "current skill not set")])
        current = get_current_skill()
        if current is None or current.path is None:
            return ValidationReport(False, [Issue("error", "skill.context.missing", "current skill not set")])
        skill_dir = current.path

        issues: List[Issue] = []
        issues += _static_checks(skill_dir, install_mode)
        # если есть критические на статике — дальше нет смысла
        if any(i.level == "error" for i in issues):
            return ValidationReport(False, issues)

        issues += _dynamic_checks(current.name, skill_dir, install_mode, probe_tools)
        ok = not any(i.level == "error" for i in issues)
        return ValidationReport(ok, issues)
