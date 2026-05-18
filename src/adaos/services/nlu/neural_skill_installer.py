from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


_SKILL_NAME = "neural_nlu_service_skill"
_MANAGED_META = ".adaos-managed.json"
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled", "none"}
_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
_log = logging.getLogger("adaos.nlu.neural.install")


def _clean_env(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().strip('"').strip("'")
    return value if value else None


def _env_value(*keys: str) -> str | None:
    for key in keys:
        raw = _clean_env(os.getenv(key))
        if raw is not None:
            return raw
    return None


def env_flag(name: str, *, default: bool, aliases: tuple[str, ...] = ()) -> bool:
    raw = _env_value(name, *aliases)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _FALSE_VALUES:
        return False
    if value in _TRUE_VALUES:
        return True
    return default


def is_neural_nlu_install_enabled() -> bool:
    """Neural NLU is prepared by default, unless explicitly disabled."""
    return env_flag(
        "ADAOS_NLU_NEURAL",
        default=True,
        aliases=("ADAOS_NLU_NEURAL_ENABLED", "ADAOS_INSTALL_NEURAL_NLU"),
    )


def _ctx_path(ctx: Any, name: str) -> Path | None:
    try:
        raw = getattr(ctx.paths, name)
    except Exception:
        return None
    try:
        value = raw() if callable(raw) else raw
    except Exception:
        return None
    if value is None:
        return None
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        return None


def _candidate_source_roots(ctx: Any, target: Path) -> list[Path]:
    candidates: list[Path] = []
    explicit = _env_value("ADAOS_NEURAL_NLU_SKILL_SOURCE")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    workspace_skill = _ctx_path(ctx, "skills_workspace_dir")
    if workspace_skill:
        candidates.append(workspace_skill / _SKILL_NAME)

    for base in (
        _ctx_path(ctx, "repo_root"),
        _ctx_path(ctx, "package_dir"),
        _ctx_path(ctx, "package_path"),
    ):
        if not base:
            continue
        candidates.append(base / "skills" / _SKILL_NAME)
        candidates.append(base.parent / "skills" / _SKILL_NAME)
        candidates.append(base.parent.parent / "skills" / _SKILL_NAME)

    try:
        cwd = Path.cwd().resolve()
        candidates.append(cwd / "skills" / _SKILL_NAME)
    except Exception:
        pass

    out: list[Path] = []
    seen: set[str] = set()
    target_resolved = target.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved == target_resolved:
            continue
        if (resolved / "skill.yaml").exists() and (resolved / "handlers" / "main.py").exists():
            out.append(resolved)
    return out


def _copy_template_tree(src: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        rel = item.relative_to(src)
        dst = target / rel
        if item.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dst)


def _managed_file_payload(target: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    for rel in ("skill.yaml", "README.md"):
        path = target / rel
        if path.exists():
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    handlers = target / "handlers"
    if handlers.exists():
        for path in sorted(handlers.rglob("*.py")):
            files[path.relative_to(target).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload: dict[str, Any] = {"files": files}
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def _write_managed_metadata(target: Path) -> dict[str, Any]:
    payload = _managed_file_payload(target)
    (target / _MANAGED_META).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _active_runtime_skill_root(ctx: Any) -> Path | None:
    skills_root = Path(ctx.paths.skills_dir())
    env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=_SKILL_NAME)
    version = env.resolve_active_version()
    if not version:
        return None
    try:
        slot = env.read_active_slot(version)
        root = env.build_slot_paths(version, slot).src_dir / "skills" / _SKILL_NAME
    except Exception:
        return None
    return root if (root / "skill.yaml").exists() else None


def _runtime_matches(ctx: Any, fingerprint: str) -> bool:
    root = _active_runtime_skill_root(ctx)
    if root is None:
        return False
    meta_path = root / _MANAGED_META
    if not meta_path.exists():
        return False
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("fingerprint") == fingerprint


def _prepare_slotted_runtime(target: Path, fingerprint: str) -> None:
    ctx = get_ctx()
    if _runtime_matches(ctx, fingerprint):
        return

    registry = None
    try:
        registry = SqliteSkillRegistry(ctx.sql)
    except Exception:
        registry = None
    mgr = SkillManager(
        git=ctx.git,
        paths=ctx.paths,
        caps=ctx.caps,
        registry=registry,
        repo=getattr(ctx, "skills_repo", None),
        bus=ctx.bus,
    )
    runtime = mgr.prepare_runtime(_SKILL_NAME, path=target, run_tests=False)
    mgr.activate_for_space(
        _SKILL_NAME,
        version=runtime.version,
        slot=runtime.slot,
        space="default",
    )


def ensure_neural_service_skill_installed() -> Path | None:
    """
    Ensure default neural service-skill exists and is staged in an active slot.

    The source is a normal registry/workspace skill tree. The hot parse bridge
    must not call this.

    Returns workspace target path when created/refreshed, otherwise None.
    """
    if not is_neural_nlu_install_enabled():
        return None

    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    target = skills_root / _SKILL_NAME

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        sources = _candidate_source_roots(ctx, target)
        if sources:
            _copy_template_tree(sources[0], target)
        elif not target.exists():
            _log.warning("neural_nlu_service_skill source not found in workspace/registry candidates")
            return None
        meta = _write_managed_metadata(target)
        _prepare_slotted_runtime(target, str(meta["fingerprint"]))
    except Exception:
        _log.warning("failed to install neural_nlu_service_skill", exc_info=True)
        return None
    return target


__all__ = [
    "ensure_neural_service_skill_installed",
    "env_flag",
    "is_neural_nlu_install_enabled",
]
