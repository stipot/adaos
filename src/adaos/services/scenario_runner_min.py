"""Minimal YAML scenario runner for the local sandbox."""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import yaml

from .ports_registry_min import call as call_port

_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")


@dataclass(slots=True)
class _InMemoryKV:
    store: Dict[str, Any]

    def get(self, key: str, default: Any | None = None) -> Any:
        return self.store.get(key, default)

    def set(self, key: str, value: Any, ttl: Any | None = None) -> None:  # noqa: D401 - signature matches kv port
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    def list(self, prefix: str = "") -> list[str]:
        return [k for k in self.store if k.startswith(prefix)]


@dataclass(slots=True)
class _InMemorySecrets:
    store: Dict[str, str]

    def get(self, name: str) -> str | None:
        return self.store.get(name)

    def put(self, name: str, value: str) -> None:
        self.store[name] = value


class _FsSkillRepository:
    def __init__(self, skills_root: Path):
        self._root = skills_root

    def get(self, skill_id: str):
        path = (self._root / skill_id).resolve()
        if not path.exists():
            return None
        return SimpleNamespace(path=path)

    def list(self) -> list[Any]:  # pragma: no cover - not used but keeps interface parity
        if not self._root.exists():
            return []
        return [child for child in self._root.iterdir() if child.is_dir()]


def _ensure_agent_context(base_dir: Path) -> None:
    from adaos.services.agent_context import AgentContext, get_ctx, set_ctx
    from adaos.services.settings import Settings
    from adaos.adapters.fs.path_provider import PathProvider
    from adaos.services.eventbus import LocalEventBus

    try:
        get_ctx()
        return
    except RuntimeError:
        pass

    settings = Settings(base_dir=base_dir.resolve(), profile="sandbox")
    paths = PathProvider(settings)
    paths.ensure_tree()

    kv = _InMemoryKV(store={})
    secrets = _InMemorySecrets(store={})

    ctx = AgentContext(
        settings=settings,
        paths=paths,
        bus=LocalEventBus(),
        proc=SimpleNamespace(),
        caps=SimpleNamespace(),
        devices=SimpleNamespace(),
        kv=kv,
        sql=SimpleNamespace(),
        secrets=secrets,
        net=SimpleNamespace(),
        updates=SimpleNamespace(),
        git=SimpleNamespace(),
        fs=SimpleNamespace(),
        sandbox=SimpleNamespace(),
    )
    object.__setattr__(ctx, "_skills_repo", _FsSkillRepository(paths.skills_dir()))
    set_ctx(ctx)


def _is_placeholder(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def _resolve_reference(expr: str, bag: Dict[str, Any]) -> Any:
    expr = expr.strip()
    if not expr:
        return None
    current: Any = bag
    for part in expr.split("."):
        key = part.strip()
        if key == "":
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            if hasattr(current, key):
                current = getattr(current, key)
            else:
                return None
        if current is None:
            return None
    return current


def _evaluate_condition(value: Any, bag: Dict[str, Any]) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and _is_placeholder(value):
        inner = value[2:-1].strip()
        if inner.startswith("not "):
            return not bool(_resolve_reference(inner[4:], bag))
        return bool(_resolve_reference(inner, bag))
    if isinstance(value, str):
        return bool(value)
    return bool(value)


def _substitute_string(template: str, bag: Dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        resolved = _resolve_reference(inner, bag)
        return "" if resolved is None else str(resolved)

    return _PLACEHOLDER_RE.sub(repl, template)


def _resolve_value(value: Any, bag: Dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_value(v, bag) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, bag) for v in value]
    if isinstance(value, str):
        if _is_placeholder(value):
            inner = value[2:-1].strip()
            if inner.startswith("not "):
                return bool(_resolve_reference(inner[4:], bag))
            return _resolve_reference(inner, bag)
        return _substitute_string(value, bag)
    return value


def _execute_step(step: Dict[str, Any], bag: Dict[str, Any]) -> None:
    if not _evaluate_condition(step.get("when"), bag):
        return

    if "set" in step:
        updates = step["set"]
        if isinstance(updates, dict):
            for key, value in updates.items():
                bag[key] = _resolve_value(value, bag)
        return

    if "do" in step:
        for sub in step.get("do", []):
            if isinstance(sub, dict):
                _execute_step(sub, bag)
        return

    route = step.get("call")
    if not route:
        return

    args = _resolve_value(step.get("args") or {}, bag)
    result = call_port(route, args)
    save_as = step.get("save_as")
    if save_as:
        bag[save_as] = result


def run_from_file(path: str) -> Dict[str, Any]:
    scenario_path = Path(path).expanduser().resolve()
    data = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}

    try:
        base_dir = scenario_path.parents[2]
    except IndexError:
        base_dir = scenario_path.parent

    _ensure_agent_context(base_dir)

    bag: Dict[str, Any] = {"vars": data.get("vars", {}) or {}}
    for step in data.get("steps", []):
        if isinstance(step, dict):
            _execute_step(step, bag)
    return bag


__all__ = ["run_from_file"]
