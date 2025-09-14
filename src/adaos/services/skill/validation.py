# src/adaos/sdk/skill_validator.py

from __future__ import annotations
import os, sys, json, subprocess, importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import copy
import yaml
from jsonschema import Draft202012Validator, ValidationError

from adaos.services.agent_context import AgentContext, get_ctx

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
    s = copy.deepcopy(spec or {})
    if s.get("description") is None:
        s["description"] = ""
    if not isinstance(s.get("dependencies"), list):
        s["dependencies"] = []
    if not isinstance(s.get("tools"), list):
        s["tools"] = []
    if not isinstance(s.get("exports"), dict):
        s["exports"] = {}
    if not isinstance(s["exports"].get("tools"), list):
        s["exports"]["tools"] = []
    if not isinstance(s.get("events"), dict):
        s["events"] = {}
    if not isinstance(s["events"].get("subscribe"), list):
        s["events"]["subscribe"] = []
    if not isinstance(s["events"].get("publish"), list):
        s["events"]["publish"] = []
    return s


def _static_checks(skill_dir: Path, install_mode: bool) -> List[Issue]:
    issues: List[Issue] = []
    sy = skill_dir / "skill.yaml"
    if not sy.exists():
        issues.append(Issue("error", "missing.skill_yaml", "skill.yaml not found", str(sy)))
        return issues
    raw = _read_yaml(sy)
    data = _normalize_spec(raw)
    try:
        schema = _load_schema()
        Draft202012Validator(schema).validate(data)
    except ValidationError as e:
        issues.append(Issue("error", "schema.invalid", f"skill.yaml schema violation: {e.message}", "skill.yaml"))
        return issues

    handler = skill_dir / "handlers" / "main.py"
    if not handler.exists():
        issues.append(Issue("error", "missing.handler", "handlers/main.py not found", str(handler)))

    tools = data.get("tools") or []
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    if len(names) != len(set(names)):
        issues.append(Issue("error", "tools.duplicate_names", "duplicate tool names in skill.yaml", "tools[]"))

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

    ev = data.get("events") or {}
    for key in ("subscribe", "publish"):
        arr = ev.get(key) or []
        for i, v in enumerate(arr):
            if not isinstance(v, str) or not v.strip():
                issues.append(Issue("error", f"events.{key}.invalid", f"events.{key}[{i}] must be non-empty string", f"events.{key}[{i}]"))
    return issues


def _dynamic_checks(skill_name: str, skill_dir: Path, install_mode: bool, probe_tools: bool) -> List[Issue]:
    """
    Импортируем handlers/main.py в ОТДЕЛЬНОМ процессе Python
    и сверяем экспорт инструментов/подписок.
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

# попытка получить новые публичные реестры (с fallback на старые)
try:
    from adaos.sdk.decorators import tools_registry, subscriptions
    mod_tools = (tools_registry.get(mod_name) or {{}})
    subs = [t for (t, _fn) in subscriptions]
except Exception:
    try:
        from adaos.sdk.decorators import _TOOLS, _SUBSCRIPTIONS
        mod_tools = (_TOOLS.get(mod_name) or {{}})
        subs = [t for (t, _fn) in _SUBSCRIPTIONS]
    except Exception:
        mod_tools, subs = {{}}, []

exports = list(mod_tools.keys())
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

    # probe_tools оставим на будущее (без исполнения)
    return issues


@dataclass(slots=True)
class SkillValidationService:
    ctx: AgentContext

    def validate(
        self,
        skill_name: Optional[str] = None,
        *,
        strict: bool = False,
        install_mode: Optional[bool] = False,
        probe_tools: bool = False,
    ) -> ValidationReport:
        """
        Валидация навыка:
        - статическая проверка skill.yaml и структуры
        - динамическая проверка экспортов/подписок через импорт handlers/main.py в отдельном процессе
        """
        ctx = self.ctx or get_ctx()

        # выбрать активный навык
        if skill_name:
            if not ctx.skill_ctx.set(skill_name, ctx.paths.skills_dir() / skill_name):
                return ValidationReport(False, [Issue("error", "skill.context.missing", f"skill '{skill_name}' not found")])

        current = ctx.skill_ctx.get()
        if current is None or getattr(current, "path", None) is None:
            return ValidationReport(False, [Issue("error", "skill.context.missing", "current skill not set")])

        skill_dir = Path(current.path)

        issues: List[Issue] = []
        issues += _static_checks(skill_dir, bool(install_mode))
        # если уже есть фатальные ошибки структуры — не продолжаем
        if any(i.level == "error" for i in issues):
            ok = not any(i.level == "error" for i in issues)
            return ValidationReport(ok, issues)

        issues += _dynamic_checks(current.name, skill_dir, bool(install_mode), bool(probe_tools))
        ok = not any(i.level == "error" for i in issues)
        return ValidationReport(ok, issues)
