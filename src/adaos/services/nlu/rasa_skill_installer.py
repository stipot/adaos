from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


_SKILL_NAME = "rasa_nlu_service_skill"
_PACKAGE = "adaos.interpreter_data"
_RESOURCE_DIR = "rasa_nlu_service_skill"
_MANAGED_META = ".adaos-managed.json"
_RASA_PORT_SUBMODULE = Path("src/adaos/integrations/rasa-port")
_DEFAULT_RASA_PORT_REQUIREMENT = "adaos-rasa-nlu @ git+https://github.com/stipot-com/rasa-port.git@main"
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled", "none"}
_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
_log = logging.getLogger("adaos.nlu.rasa.install")


def _clean_env(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().strip('"').strip("'")
    return value if value else None


def _dotenv_candidates() -> list[Path]:
    candidates: list[Path] = []
    shared = _clean_env(os.getenv("ADAOS_SHARED_DOTENV_PATH"))
    if shared:
        candidates.append(Path(shared).expanduser())
    try:
        cwd = Path.cwd().resolve()
        candidates.extend(base / ".env" for base in (cwd, *cwd.parents))
    except Exception:
        pass
    return candidates


def _read_env_file_value(key: str) -> str | None:
    for candidate in _dotenv_candidates():
        try:
            if not candidate.exists():
                continue
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == key:
                    return _clean_env(value)
        except Exception:
            continue
    return None


def _env_value(*keys: str) -> str | None:
    for key in keys:
        raw = _clean_env(os.getenv(key))
        if raw is not None:
            return raw
        raw = _read_env_file_value(key)
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


def is_rasa_nlu_enabled() -> bool:
    """Rasa NLU is optional, but enabled by default."""
    return env_flag("ADAOS_NLU_RASA", default=True, aliases=("ADAOS_NLU_RASA_ENABLED",))


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


def _is_rasa_port_checkout(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        resolved = path
    return (
        (resolved / "pyproject.toml").exists()
        and (resolved / "adaos_rasa_nlu").exists()
        and (resolved / "rasa").exists()
    )


def _local_rasa_port_path(ctx: Any) -> Path | None:
    candidates: list[Path] = []
    env_path = _env_value("ADAOS_RASA_PORT_PATH")
    if env_path:
        candidates.append(Path(env_path))

    package_dir = _ctx_path(ctx, "package_path") or _ctx_path(ctx, "package_dir")
    if package_dir:
        candidates.append(package_dir / "integrations" / "rasa-port")
        candidates.append(package_dir / "src" / "adaos" / "integrations" / "rasa-port")

    repo_root = _ctx_path(ctx, "repo_root")
    if repo_root:
        candidates.append(repo_root / "src" / "adaos" / "integrations" / "rasa-port")

    try:
        cwd = Path.cwd().resolve()
        candidates.append(cwd / "src" / "adaos" / "integrations" / "rasa-port")
    except Exception:
        pass

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if _is_rasa_port_checkout(resolved):
            return resolved
    return None


def _repo_root_candidates(ctx: Any) -> list[Path]:
    candidates: list[Path] = []
    repo_root = _ctx_path(ctx, "repo_root")
    if repo_root:
        candidates.append(repo_root)

    package_dir = _ctx_path(ctx, "package_path") or _ctx_path(ctx, "package_dir")
    if package_dir:
        for candidate in (
            package_dir,
            package_dir.parent,
            package_dir.parents[1] if len(package_dir.parents) > 1 else None,
        ):
            if candidate is not None:
                candidates.append(candidate)

    try:
        cwd = Path.cwd().resolve()
        candidates.extend((cwd, *cwd.parents))
    except Exception:
        pass

    seen: set[str] = set()
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if (resolved / ".gitmodules").exists():
            roots.append(resolved)
    return roots


def _declares_rasa_port_submodule(repo_root: Path) -> bool:
    try:
        text = (repo_root / ".gitmodules").read_text(encoding="utf-8")
    except Exception:
        return False
    needle = _RASA_PORT_SUBMODULE.as_posix().lower()
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip().lower() == "path" and value.strip().lower() == needle:
            return True
    return False


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except Exception:
        return str(left).lower() == str(right).lower()


def _rasa_port_submodule_needs_init(repo_root: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "submodule",
                "status",
                "--",
                _RASA_PORT_SUBMODULE.as_posix(),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False

    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return False
    return line[0].startswith("-")


def _ensure_rasa_port_submodule_checkout(ctx: Any) -> Path | None:
    existing = _local_rasa_port_path(ctx)

    for repo_root in _repo_root_candidates(ctx):
        if not _declares_rasa_port_submodule(repo_root):
            continue
        submodule_path = (repo_root / _RASA_PORT_SUBMODULE).resolve()
        if existing is not None and not _same_path(existing, submodule_path):
            continue
        if not (repo_root / ".git").exists() or shutil.which("git") is None:
            if _is_rasa_port_checkout(submodule_path):
                return submodule_path
            continue

        needs_checkout = not _is_rasa_port_checkout(submodule_path)
        needs_init = _rasa_port_submodule_needs_init(repo_root)
        if not needs_checkout and not needs_init:
            return submodule_path

        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                    "--",
                    _RASA_PORT_SUBMODULE.as_posix(),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except Exception as exc:
            _log.warning("failed to initialize rasa-port submodule at %s: %s", submodule_path, exc)
            continue

        if _is_rasa_port_checkout(submodule_path):
            return submodule_path
        _log.warning("rasa-port submodule initialized but checkout is incomplete at %s", submodule_path)

    return existing


def _path_requirement(path: Path) -> str:
    try:
        return path.expanduser().resolve().as_uri()
    except Exception:
        return str(path)


def _rasa_port_dependency_args(ctx: Any) -> list[str]:
    local_path = _ensure_rasa_port_submodule_checkout(ctx)
    if local_path:
        return ["--no-deps", "-e", _path_requirement(local_path)]
    requirement = _env_value("ADAOS_RASA_PORT_REQUIREMENT") or _DEFAULT_RASA_PORT_REQUIREMENT
    return ["--no-deps", requirement]


def _write_rasa_port_dependency(target: Path, ctx: Any) -> list[str]:
    manifest_path = target / "skill.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict):
        manifest = {}
    dependencies = _rasa_port_dependency_args(ctx)
    manifest["dependencies"] = dependencies
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return dependencies


def _managed_file_payload(target: Path, dependencies: list[str]) -> dict[str, Any]:
    files: dict[str, str] = {}
    for rel in ("skill.yaml", "requirements.in", "README.md"):
        path = target / rel
        if path.exists():
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    handlers = target / "handlers"
    if handlers.exists():
        for path in sorted(handlers.rglob("*.py")):
            files[path.relative_to(target).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {"dependencies": list(dependencies), "files": files}
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    payload["fingerprint"] = fingerprint
    return payload


def _write_managed_metadata(target: Path, dependencies: list[str]) -> dict[str, Any]:
    payload = _managed_file_payload(target, dependencies)
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


def ensure_rasa_service_skill_installed() -> Path | None:
    """
    Ensure default Rasa NLU service-skill exists and is staged in an active slot.

    Returns workspace target path when created/refreshed, otherwise None.
    """
    if not is_rasa_nlu_enabled():
        return None

    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    target = skills_root / _SKILL_NAME

    try:
        src_dir = resources.files(_PACKAGE) / _RESOURCE_DIR
    except Exception:
        _log.warning("failed to locate packaged rasa_nlu_service_skill template", exc_info=True)
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with resources.as_file(src_dir) as src:
            _copy_template_tree(Path(src), target)
        dependencies = _write_rasa_port_dependency(target, ctx)
        meta = _write_managed_metadata(target, dependencies)
        _prepare_slotted_runtime(target, str(meta["fingerprint"]))
    except Exception:
        _log.warning("failed to install rasa_nlu_service_skill", exc_info=True)
        return None
    return target
