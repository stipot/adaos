# src\adaos\services\skill\manager.py
from __future__ import annotations

import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import yaml
from adaos.adapters.git.workspace import wait_for_materialized
from adaos.services.workspace_registry import upsert_workspace_registry_entry

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, SkillRepository, SkillRegistry
from adaos.ports.paths import PathProvider
from adaos.services.eventbus import emit
from adaos.ports import Capabilities
from adaos.services.fs.safe_io import remove_tree
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.services.git.workspace_guard import ensure_clean
from adaos.services.settings import Settings
from adaos.services.agent_context import AgentContext, get_ctx, use_ctx
from adaos.services.skill.dependency_requirements import resolve_skill_dependency_args
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment, SkillSlotPaths
from adaos.services.skill.tests_runner import TestResult, run_tests
from adaos.skills.runtime_runner import execute_tool
from adaos.services.skill.validation import SkillValidationService, ValidationReport
from adaos.services.crypto.secrets_service import SecretsService
from adaos.services.skill.secrets_backend import SkillSecretsBackend
from adaos.services.skill.resolver import SkillPathResolver
from adaos.services.capacity import install_skill_in_capacity, uninstall_skill_from_capacity
from adaos.services.semver import bump_version
from adaos.services.skill.version_policy import RESERVED_DATA_MIGRATION_FILE, bump_index, effective_skill_bump
import ast

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")
_log = logging.getLogger("adaos.skill.manager")
_SKILL_MANIFEST_NAMES = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")


def _env_type() -> str:
    return str(os.getenv("ENV_TYPE", "prod") or "prod").strip().lower()


def _default_webspace_id() -> str:
    from adaos.services.yjs.webspace import default_webspace_id

    return default_webspace_id()


def _payload_webspace_id(payload: Mapping[str, Any] | None) -> str:
    if isinstance(payload, Mapping):
        token = str(payload.get("webspace_id") or "").strip()
        if token:
            return token
    return _default_webspace_id()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "write", "mutate"}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _skill_tool_yjs_governance(tool_spec: Mapping[str, Any] | None) -> dict[str, Any]:
    spec = _mapping_or_empty(tool_spec)
    governance = dict(_mapping_or_empty(spec.get("yjs_governance") or spec.get("yjs")))
    side_effects = (
        governance.get("side_effects")
        or spec.get("side_effects")
        or spec.get("sideEffects")
        or spec.get("effects")
        or spec.get("effect")
        or ""
    )
    if side_effects:
        governance["side_effects"] = str(side_effects).strip().lower()
    if "read_only" not in governance:
        governance["read_only"] = bool(spec.get("read_only") or spec.get("readonly"))
    return governance


def _skill_tool_yjs_root_names(tool_spec: Mapping[str, Any] | None) -> list[str]:
    governance = _skill_tool_yjs_governance(tool_spec)
    raw = governance.get("root_names") or governance.get("roots") or []
    if not isinstance(raw, (list, tuple)):
        return []
    roots: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in roots:
            roots.append(token)
    return roots


def _skill_tool_yjs_read_only(tool: str, payload: Mapping[str, Any] | None, tool_spec: Mapping[str, Any] | None) -> bool:
    body = payload if isinstance(payload, Mapping) else {}
    governance = _skill_tool_yjs_governance(tool_spec)
    side_effects = str(governance.get("side_effects") or "").strip().lower()
    declared_read_only = bool(governance.get("read_only")) or side_effects in {
        "none",
        "read",
        "read-only",
        "read_only",
        "readonly",
    }
    mutating_keys = governance.get("mutating_payload_keys")
    if not isinstance(mutating_keys, (list, tuple)):
        mutating_keys = ("project", "write", "mutate", "apply")
    if declared_read_only:
        return not any(_truthy(body.get(str(key))) for key in mutating_keys)

    # Backward-compatible diagnostic convention for existing resolved manifests
    # that predate explicit side_effects metadata.
    token = str(tool or "").strip().lower()
    if token in {"get_snapshot", "snapshot", "status", "diagnostics"} or token.startswith(("get_", "list_", "read_", "inspect_")):
        return not any(_truthy(body.get(key)) for key in ("project", "write", "mutate", "apply"))
    return False


def _admit_skill_tool_yjs_work(
    name: str,
    tool: str,
    payload: Mapping[str, Any] | None,
    tool_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from adaos.services.yjs.owner_guard import admit_skill_tool

        return admit_skill_tool(
            skill_name=name,
            tool=tool,
            payload=payload,
            read_only=_skill_tool_yjs_read_only(tool, payload, tool_spec),
            root_names=_skill_tool_yjs_root_names(tool_spec),
        )
    except Exception:
        _log.debug("failed to apply YJS owner guard for skill=%s tool=%s", name, tool, exc_info=True)
        return {"allowed": True, "governed": False, "reason": "owner_guard_unavailable"}


def _skill_tool_yjs_denied_result(
    *,
    name: str,
    tool: str,
    payload: Mapping[str, Any] | None,
    admission: Mapping[str, Any],
) -> dict[str, Any]:
    retry_after_s = max(0.0, float(admission.get("retry_after_s") or 0.0))
    reason = str(admission.get("reason") or "owner_quarantined").strip() or "owner_quarantined"
    webspace_id = str(admission.get("webspace_id") or _payload_webspace_id(payload)).strip() or "default"
    owner = str(admission.get("owner") or f"skill:{name}").strip()
    target_node_id = ""
    if isinstance(payload, Mapping):
        target_node_id = str(payload.get("target_node_id") or "").strip()
    return {
        "ok": False,
        "degraded": True,
        "unavailable": True,
        "source": "yjs_owner_guard",
        "error": "skill_owner_quarantined",
        "reason": reason,
        "retryable": True,
        "retry_after_s": retry_after_s,
        "webspace_id": webspace_id,
        "owner": owner,
        "tool": f"{name}:{tool}",
        "target_node_id": target_node_id or None,
        "quarantine": admission.get("quarantine") if isinstance(admission.get("quarantine"), Mapping) else None,
        "summary": {
            "value": "quarantined",
            "status": "degraded",
            "label": "Skill owner quarantined",
            "description": reason,
            "selected_node_id": target_node_id or None,
        },
    }


def _skill_quarantine_event(
    *,
    name: str,
    tool: str,
    payload: Mapping[str, Any] | None,
    admission: Mapping[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    webspace_id = str(admission.get("webspace_id") or _payload_webspace_id(payload)).strip() or "default"
    owner = str(admission.get("owner") or f"skill:{name}").strip()
    reason = str(admission.get("reason") or "owner_quarantined").strip() or "owner_quarantined"
    retry_after_s = max(0.0, float(admission.get("retry_after_s") or 0.0))
    quarantine = admission.get("quarantine") if isinstance(admission.get("quarantine"), Mapping) else {}
    target_node_id = ""
    if isinstance(payload, Mapping):
        target_node_id = str(payload.get("target_node_id") or "").strip()
    return {
        "event": "skill.quarantine",
        "schema": "adaos.skill_quarantine.v1",
        "ts": now,
        "updated_at": time.time(),
        "skill": name,
        "owner": owner,
        "blocked_tool": tool,
        "webspace_id": webspace_id,
        "target_node_id": target_node_id or None,
        "reason": reason,
        "retry_after_s": retry_after_s,
        "ttl_s": retry_after_s,
        "policy_state": str(admission.get("policy_state") or quarantine.get("policy_state") or "block").strip() or "block",
        "trigger": str(quarantine.get("trigger") or "").strip() or None,
        "path": str(quarantine.get("path") or "").strip() or None,
        "source": str(quarantine.get("source") or "").strip() or None,
        "channel": str(quarantine.get("channel") or "").strip() or None,
        "quarantine_until": float(quarantine.get("quarantine_until") or 0.0) or None,
    }


def _append_skill_quarantine_log(skill_memory_path: Path, event: Mapping[str, Any]) -> None:
    try:
        base = Path(skill_memory_path)
        if base.name == "skill_env.json" and base.parent.name == "db":
            base = base.parent.parent
        elif base.suffix:
            base = base.parent
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "quarantine.jsonl"
        payload = dict(event)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)[:32768] + "\n")
    except Exception:
        _log.debug("failed to append skill quarantine log skill_memory=%s", skill_memory_path, exc_info=True)


def _quarantine_hook_tool_name(tools: Mapping[str, Any], blocked_tool: str) -> str | None:
    if str(blocked_tool or "").strip() in {"onQuarantine", "on_quarantine"}:
        return None
    for candidate in ("onQuarantine", "on_quarantine"):
        if candidate in tools:
            return candidate
    return None


def _quarantine_hook_timeout(tool_spec: Mapping[str, Any]) -> float:
    raw = tool_spec.get("timeout_seconds")
    try:
        if raw is not None:
            return max(0.5, min(10.0, float(raw)))
    except Exception:
        pass
    try:
        return max(0.5, min(10.0, float(os.getenv("ADAOS_YJS_OWNER_QUARANTINE_HOOK_TIMEOUT_S") or "5")))
    except Exception:
        return 5.0


def _invoke_skill_quarantine_hook(
    *,
    ctx: AgentContext,
    name: str,
    tools: Mapping[str, Any],
    blocked_tool: str,
    skill_dir: Path,
    skill_env_path: Path,
    skill_memory_path: Path,
    secrets_path: Path,
    extra_paths: list[Path],
    event: Mapping[str, Any],
    admission: Mapping[str, Any],
) -> dict[str, Any]:
    hook_name = _quarantine_hook_tool_name(tools, blocked_tool)
    if not hook_name:
        return {"called": False, "reason": "hook_not_declared"}
    hook_spec = tools.get(hook_name)
    if not isinstance(hook_spec, Mapping):
        return {"called": False, "reason": "hook_spec_invalid"}
    module = hook_spec.get("module")
    attr = hook_spec.get("callable") or hook_name
    hook_payload = {
        "event": dict(event),
        "ttl_s": event.get("ttl_s"),
        "retry_after_s": event.get("retry_after_s"),
        "reason": event.get("reason"),
        "blocked_tool": blocked_tool,
        "skill": name,
        "webspace_id": event.get("webspace_id"),
        "owner": event.get("owner"),
        "quarantine": admission.get("quarantine") if isinstance(admission.get("quarantine"), Mapping) else None,
    }
    previous = ctx.skill_ctx.get()
    prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
    prev_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH")
    prev_secrets = ctx.secrets
    timeout_s = _quarantine_hook_timeout(hook_spec)

    def _call_hook() -> Any:
        with use_ctx(ctx):
            return execute_tool(
                skill_dir,
                module=module,
                attr=attr,
                payload=hook_payload,
                extra_paths=extra_paths,
            )

    try:
        if not ctx.skill_ctx.set(name, skill_dir):
            return {"called": False, "reason": "skill_context_unavailable"}
        ctx.secrets = SecretsService(SkillSecretsBackend(secrets_path), ctx.caps)
        os.environ["ADAOS_SKILL_ENV_PATH"] = str(skill_env_path)
        os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(skill_memory_path)

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
        from contextvars import copy_context

        with ThreadPoolExecutor(max_workers=1) as pool:
            ctxvars = copy_context()
            future = pool.submit(lambda: ctxvars.run(_call_hook))
            try:
                result = future.result(timeout=timeout_s)
            except FuturesTimeoutError as exc:
                future.cancel()
                raise TimeoutError(f"quarantine hook '{hook_name}' timed out after {timeout_s} seconds") from exc
        return {"called": True, "ok": True, "hook": hook_name, "result": result}
    except Exception as exc:
        _log.warning("skill quarantine hook failed skill=%s hook=%s: %s", name, hook_name, exc)
        return {"called": True, "ok": False, "hook": hook_name, "error": str(exc)}
    finally:
        ctx.secrets = prev_secrets
        if previous is None:
            ctx.skill_ctx.clear()
        else:
            ctx.skill_ctx.set(previous.name, Path(previous.path))
        if prev_env is None:
            os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
        else:
            os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env
        if prev_memory is None:
            os.environ.pop("ADAOS_SKILL_MEMORY_PATH", None)
        else:
            os.environ["ADAOS_SKILL_MEMORY_PATH"] = prev_memory


@dataclass(slots=True)
class RuntimeInstallResult:
    name: str
    version: str
    slot: str
    resolved_manifest: Path
    tests: Dict[str, TestResult]
    data_migration: Dict[str, Any] | None = None
    lifecycle: Dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class PolicyDefaults:
    timeout_seconds: float
    retry_count: int
    telemetry_enabled: bool
    sandbox_memory_mb: int | None = None
    sandbox_cpu_seconds: float | None = None


class SkillManager:
    def __init__(
        self,
        *,
        git: GitClient,  # Deprecated. TODO Move to ctx
        paths: PathProvider,  # Deprecated. TODO Move to ctx
        caps: Capabilities,
        settings: Settings | None = None,
        registry: SkillRegistry = None,
        repo: SkillRepository | None = None,  # Deprecated. TODO Move to ctx
        bus: EventBus | None = None,
    ):
        self.reg = registry
        self.bus = bus
        self.caps = caps
        self.settings = settings
        self.ctx: AgentContext = get_ctx()

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "skills.manage")
        return self.ctx.skills_repo.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "skills.manage")
        self.ctx.skills_repo.ensure()
        return self.ctx.skills_repo.list()

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        return self.ctx.skills_repo.get(skill_id)

    def sync(self, *, force: bool | None = None) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        self.ctx.skills_repo.ensure()
        root = self.ctx.paths.workspace_dir()
        names = [r.name for r in self.reg.list()]
        scenario_names: list[str] = []
        try:
            from adaos.adapters.db import SqliteScenarioRegistry

            scenario_names = [r.name for r in SqliteScenarioRegistry(self.ctx.sql).list()]
        except Exception:
            scenario_names = []

        # Workspace repo hosts both /skills and /scenarios under a single sparse-checkout.
        # Keep both sets in the sparse pattern list to avoid "disappearing" directories
        # when syncing one kind.
        prefixed = [
            ".gitignore",
            "registry.json",
            "schemas",
            *[f"skills/{n}" for n in names],
            *[f"scenarios/{n}" for n in scenario_names],
        ]
        effective_force = (_env_type() != "dev") if force is None else bool(force)
        if effective_force:
            stash_ref = self.ctx.git.stash_push(
                str(root),
                "adaos:auto-stash forced skill sync",
                include_untracked=True,
            )
            _log.warning(
                "forced skill sync requested env_type=%s repo=%s stash=%s",
                _env_type(),
                str(root),
                str(stash_ref or "-"),
            )
        else:
            ensure_clean(self.ctx.git, str(root), prefixed)
        self.ctx.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.ctx.git.sparse_set(str(root), prefixed, no_cone=True)
        self.ctx.git.pull(str(root))
        emit(self.bus, "skill.sync", {"count": len(names)}, "skill.mgr")

    def install(
        self,
        name: str,
        pin: str | None = None,
        validate: bool = True,
        strict: bool = True,
        probe_tools: bool = False,
        safe: bool = False,
    ) -> tuple[SkillMeta, Optional[object]]:
        """
        Возвращает (meta, report|None). При strict и ошибках валидации можно выбрасывать исключение.
        """
        self.caps.require("core", "skills.manage")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if not test_mode:
            resolver = getattr(self.ctx.skills_repo, "resolve_install_name", None)
            if callable(resolver):
                name = str(resolver(name)).strip() or name

        # 1) регистрируем (идемпотентно)
        self.reg.register(name, pin=pin)
        # 2) в тестах/без .git — только реестр
        if test_mode:
            return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
        # 3) при безопасной установке проверяем, что рабочее дерево чисто под skills/*
        root = self.ctx.paths.workspace_dir()
        if safe and (root / ".git").exists():
            ensure_clean(self.ctx.git, str(root), ["skills"])
        # 4) mono-only установка через репозиторий (sparse-add + pull)
        meta = self.ctx.skills_repo.install(name, branch=None)
        """ if not validate:
            return meta, None """
        report = SkillValidationService(self.ctx).validate(meta.id.value, strict=strict, probe_tools=probe_tools)
        if strict and not report.ok:
            # опционально можно откатывать установку:
            # self.ctx.skills_repo.uninstall(meta.id.value)
            # и/или пробрасывать исключение
            return meta, report

        return meta, report  # return f"installed: {name}"

    def validate_skill(
        self,
        name: str,
        *,
        strict: bool = True,
        probe_tools: bool = False,
        source: str = "workspace",  # "dev" | "workspace" | "installed" (строка для простоты)
        path: Path | None = None,  # явный путь имеет приоритет
    ) -> ValidationReport:
        """Run validation for a skill via the service layer."""

        self.caps.require("core", "skills.manage")
        ctx: AgentContext = self.ctx
        previous = ctx.skill_ctx.get()
        try:
            svc = SkillValidationService(ctx)

            if path is None:
                # собираем резолвер из путей контекста
                resolver = SkillPathResolver(
                    dev_root=ctx.paths.dev_skills_dir(),
                    workspace_root=ctx.paths.skills_workspace_dir(),
                )
                root_path = resolver.resolve(name, space=source)  # FileNotFoundError bubbling up -> handled by caller
            else:
                root_path = Path(path).resolve()

            report = svc.validate_path(
                root_path,
                name=name,
                strict=strict,
                probe_tools=probe_tools,
            )
        finally:
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
        return report

    def run_skill_tests(
        self,
        name: str,
        *,
        source: str = "workspace",  # "dev" | "workspace" | "installed"
        path: Path | None = None,  # явный путь имеет приоритет
    ) -> Dict[str, TestResult]:
        """Execute runtime tests without preparing a new slot.
        Location-agnostic via resolver: dev/workspace/installed or explicit path.
        NOTE: semantics unchanged — tests rely on installed versions/slots.
        """

        # 1) resolve skill_dir via explicit path or resolver (space)
        if path is not None:
            skill_dir = Path(path).resolve()
            if not skill_dir.exists() or not skill_dir.is_dir():
                raise FileNotFoundError(f"skill path not found or not a directory: {skill_dir}")
        else:
            from .resolver import SkillPathResolver

            resolver = SkillPathResolver(
                dev_root=self.ctx.paths.dev_skills_dir(),
                workspace_root=self.ctx.paths.skills_workspace_dir(),
            )
            skill_dir = resolver.resolve(name, space=source if source in ("dev", "workspace") else "workspace")

        # 2) Runtime store lives under `<skills_root>/.runtime/<skill>/v<major>.<minor>/data/...`,
        # i.e. next to the sources but not inside the git-tracked skill folder.
        env = SkillRuntimeEnvironment(skills_root=skill_dir.parent, skill_name=name)
        version = env.resolve_active_version()
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        package_root = Path(package_dir).resolve().parent if package_dir else None

        interpreter: Path | None = None
        python_paths: list[str] = []
        skill_source = skill_dir

    # ------------------------------------------------------------------
    # Runtime update helpers (workspace/dev, used by DEBUG flows and LLM-assisted workflows)
    # ------------------------------------------------------------------

    def runtime_update(
        self,
        name: str,
        *,
        space: str = "workspace",
        path: Path | None = None,
    ) -> Dict[str, Any]:
        """
        Synchronise an existing runtime slot with latest sources and tool
        declarations from the corresponding workspace or DEV skill folder.

        This is intentionally lightweight: it does not change versions,
        slots or install dependencies – it only propagates changed source
        files and extends ``resolved.manifest.json`` with tools defined in
        ``skill.yaml`` that are missing from the active slot manifest.
        """
        ctx = self.ctx
        space_normalized = (space or "workspace").strip().lower()
        if space_normalized not in ("workspace", "dev"):
            raise ValueError("space must be 'workspace' or 'dev'")

        # Resolve skill directory and runtime environment depending on space.
        if space_normalized == "dev":
            skill_dir, source_kind = self._resolve_runtime_update_source(
                name,
                space=space_normalized,
                path=path,
            )
            status = self.dev_runtime_status(name)
            env = self._runtime_env_dev(name)
        else:
            skill_dir, source_kind = self._resolve_runtime_update_source(
                name,
                space=space_normalized,
                path=path,
            )
            status = self.runtime_status(name)
            env = self._runtime_env(name)

        if not skill_dir.exists():
            return {
                "ok": False,
                "reason": "skill_dir_missing",
                "skill": name,
                "space": space_normalized,
                "source": source_kind,
                "source_path": str(skill_dir),
            }

        version = status.get("version")
        active_slot = status.get("active_slot")
        resolved_manifest = status.get("resolved_manifest")
        if not version or not resolved_manifest:
            return {"ok": False, "reason": "no_active_runtime", "skill": name, "space": space_normalized}

        try:
            current_link = env.ensure_current_link(version)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "ensure_current_failed",
                "skill": name,
                "space": space_normalized,
                "error": str(exc),
            }

        runtime_skill_root = current_link / "src" / "skills" / name
        if not runtime_skill_root.exists():
            return {
                "ok": False,
                "reason": "runtime_src_missing",
                "skill": name,
                "space": space_normalized,
                "path": str(runtime_skill_root),
            }

        # 1) Sync source files (py/json/yaml/md) from workspace/DEV into runtime slot.
        changed_files = self._runtime_sync_sources(
            skill_dir,
            runtime_skill_root,
            force=(source_kind == "repo_workspace"),
        )

        # 2) Sync tool declarations into resolved.manifest.json from skill.yaml.
        manifest_path = Path(resolved_manifest)
        tools_added: list[str] = []
        if manifest_path.exists():
            try:
                tools_added = self._runtime_sync_manifest_tools(name, manifest_path, skill_dir)
                tools_added.extend(
                    self._runtime_sync_manifest_tools_from_handlers(
                        manifest_path=manifest_path,
                        handlers_main=skill_dir / "handlers" / "main.py",
                    )
                )
            except Exception as exc:
                # Do not treat manifest sync as fatal for runtime_update; report in payload.
                return {
                    "ok": False,
                    "reason": "manifest_sync_failed",
                    "skill": name,
                    "space": space_normalized,
                    "error": str(exc),
                }

        payload = {
            "ok": True,
            "skill": name,
            "space": space_normalized,
            "version": version,
            "slot": active_slot,
            "source": source_kind,
            "source_path": str(skill_dir),
            "files": changed_files,
            "tools_added": tools_added,
        }

        # Keep local node capacity in sync so UI layers (e.g. web desktop)
        # can discover skills/apps from node.yaml even when only runtime_update
        # is used (e.g. `adaos setup update`).
        try:
            install_skill_in_capacity(name, str(version), active=True, dev=(space_normalized == "dev"))
            try:
                from adaos.services.node_config import load_config
                from adaos.services.capacity import get_local_capacity
                from adaos.services.registry.subnet_directory import get_directory

                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_skill_capacity(conf.node_id, cap.get("skills") or [])
            except Exception:
                pass
        except Exception:
            pass

        try:
            emit(
                self.bus,
                "dev.skill.runtime.updated",
                payload,
                actor="skill.manager",
            )
        except Exception:
            # Best-effort: event emission must not break update.
            pass

        return payload

    def _resolve_runtime_update_source(
        self,
        name: str,
        *,
        space: str,
        path: Path | None = None,
    ) -> tuple[Path, str]:
        if path is not None:
            return Path(path).resolve(), "explicit"

        if space == "dev":
            root = self.ctx.paths.dev_skills_dir()
            root = root() if callable(root) else root
            return (Path(root) / name).resolve(), "dev"

        root = self.ctx.paths.skills_workspace_dir()
        root = root() if callable(root) else root
        workspace_skill = (Path(root) / name).resolve()
        if workspace_skill.exists():
            return workspace_skill, "workspace"

        repo_skill = self._repo_workspace_skill_dir(name)
        if repo_skill is not None:
            return repo_skill, "repo_workspace"
        return workspace_skill, "workspace"

    def _repo_workspace_skill_dir(self, name: str) -> Path | None:
        try:
            repo_root_attr = getattr(self.ctx.paths, "repo_root", None)
            repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
            if not repo_root:
                return None
            candidate = Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "skills" / name
            if candidate.exists():
                return candidate
        except Exception:
            return None
        return None

    def _runtime_sync_sources(self, source_root: Path, runtime_root: Path, *, force: bool = False) -> list[str]:
        """
        Copy changed source files from ``source_root`` into ``runtime_root``.

        Only *.py, *.json, *.yml, *.yaml, *.md are considered; auxiliary
        folders such as .git, __pycache__, .runtime are skipped.
        """
        exts = {".py", ".json", ".yml", ".yaml", ".md"}
        changed: list[str] = []
        if not source_root.exists() or not runtime_root.exists():
            return changed

        skip_dirs = {".git", "__pycache__", ".runtime"}

        for src in source_root.rglob("*"):
            if not src.is_file():
                continue
            if src.suffix.lower() not in exts:
                continue
            if any(part in skip_dirs for part in src.parts):
                continue
            rel = src.relative_to(source_root)
            dst = runtime_root / rel
            try:
                if dst.exists() and not force:
                    src_stat = src.stat()
                    dst_stat = dst.stat()
                    same_size = int(src_stat.st_size) == int(dst_stat.st_size)
                    newer_source = float(src_stat.st_mtime) > float(dst_stat.st_mtime)
                    if same_size and not newer_source:
                        try:
                            if src.read_bytes() == dst.read_bytes():
                                continue
                        except OSError:
                            pass
                    elif not newer_source:
                        continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                changed.append(rel.as_posix())
            except OSError:
                continue
        return changed

    def _runtime_sync_manifest_tools(
        self,
        name: str,
        manifest_path: Path,
        skill_dir: Path,
    ) -> list[str]:
        """
        Extend the active slot manifest's ``tools`` section with any tools
        defined in ``skill.yaml`` that are not yet present.
        """
        if not manifest_path.exists():
            return []

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or {}
        if not isinstance(tools, dict):
            tools = {}
        policy = data.get("policy") or {}
        default_timeout = float(policy.get("timeout_seconds") or 30.0)
        default_retries = int(policy.get("retry_count") or 1)

        # Use the first existing tool (if any) as template for permissions/secrets.
        template = next(iter(tools.values()), None)
        base_permissions = template.get("permissions") if isinstance(template, dict) else None
        base_secrets = template.get("secrets") if isinstance(template, dict) else []

        skill_yaml = skill_dir / "skill.yaml"
        if not skill_yaml.exists():
            return []
        manifest = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
        skill_tools = manifest.get("tools") or []
        if not isinstance(skill_tools, list):
            return []

        added: list[str] = []
        for entry in skill_tools:
            if not isinstance(entry, dict):
                continue
            tool_name = entry.get("name")
            if not tool_name:
                continue
            if tool_name in tools:
                existing = tools.get(tool_name)
                if isinstance(existing, dict):
                    changed = False
                    for meta_key in ("side_effects", "read_only", "yjs_governance"):
                        if meta_key in entry and existing.get(meta_key) != entry.get(meta_key):
                            existing[meta_key] = entry.get(meta_key)
                            changed = True
                    if changed:
                        added.append(tool_name)
                continue
            raw_entry = entry.get("entry") or ""
            if ":" not in raw_entry:
                continue
            module, attr = raw_entry.split(":", 1)
            input_schema = entry.get("input_schema") or {}
            output_schema = entry.get("output_schema") or {}
            tools[tool_name] = {
                "name": tool_name,
                "module": module,
                "callable": attr,
                "timeout_seconds": default_timeout,
                "retries": default_retries,
                "schema": {
                    "input": input_schema,
                    "output": output_schema,
                },
                "permissions": base_permissions,
                "secrets": base_secrets,
            }
            for meta_key in ("side_effects", "read_only", "yjs_governance"):
                if meta_key in entry:
                    tools[tool_name][meta_key] = entry.get(meta_key)
            added.append(tool_name)

        if added:
            data["tools"] = tools
            tmp = manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, manifest_path)

        return added

    @staticmethod
    def _discover_tools_from_handlers(handlers_main: Path) -> list[tuple[str, str]]:
        """
        Discover ``@tool``-decorated handlers in ``handlers/main.py``.

        Returns list of (tool_name, func_name). Best-effort: parse/read errors yield empty list.
        """
        try:
            source = handlers_main.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            tree = ast.parse(source, filename=str(handlers_main))
        except SyntaxError:
            return []

        discovered: list[tuple[str, str]] = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            func_name = node.name
            for dec in node.decorator_list:
                # Match @tool("name") or @tool(name="..."), and bare @tool.
                tool_name: str | None = None
                if isinstance(dec, ast.Call):
                    target = dec.func
                    is_tool = False
                    if isinstance(target, ast.Name) and target.id == "tool":
                        is_tool = True
                    elif isinstance(target, ast.Attribute) and target.attr == "tool":
                        is_tool = True
                    if not is_tool:
                        continue
                    if dec.args:
                        arg = dec.args[0]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            tool_name = arg.value
                    if not tool_name:
                        for kw in dec.keywords or []:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                tool_name = kw.value.value
                                break
                elif isinstance(dec, ast.Name) and dec.id == "tool":
                    tool_name = func_name
                if tool_name:
                    discovered.append((tool_name, func_name))
        return discovered

    def _runtime_sync_manifest_tools_from_handlers(
        self,
        *,
        manifest_path: Path,
        handlers_main: Path,
    ) -> list[str]:
        """
        Extend ``resolved.manifest.json`` tools using ``@tool`` decorators from handlers.

        Keeps runtime manifests up-to-date during local DEBUG sync without writing back
        into the git-tracked workspace skill sources.
        """
        if not manifest_path.exists():
            return []

        discovered = self._discover_tools_from_handlers(handlers_main)
        if not discovered:
            return []

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or {}
        if not isinstance(tools, dict):
            tools = {}
        policy = data.get("policy") or {}
        default_timeout = float(policy.get("timeout_seconds") or 30.0)
        default_retries = int(policy.get("retry_count") or 1)

        template = next(iter(tools.values()), None)
        base_permissions = template.get("permissions") if isinstance(template, dict) else None
        base_secrets = template.get("secrets") if isinstance(template, dict) else []

        added: list[str] = []
        for tool_name, func_name in discovered:
            if not tool_name or tool_name in tools:
                continue
            tools[tool_name] = {
                "name": tool_name,
                "module": "handlers.main",
                "callable": func_name,
                "timeout_seconds": default_timeout,
                "retries": default_retries,
                "schema": {
                    "input": {"type": "object", "properties": {}},
                    "output": {"type": "object", "properties": {}},
                },
                "permissions": base_permissions,
                "secrets": base_secrets,
            }
            added.append(tool_name)

        if added:
            data["tools"] = tools
            tmp = manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, manifest_path)

        return added

    # ------------------------------------------------------------------
    # Workspace helpers for dev/runtime sync
    # ------------------------------------------------------------------
    def sync_skill_yaml_tools_from_handlers(self, name: str, *, space: str = "workspace") -> dict[str, Any]:
        """
        Ensure that ``skill.yaml.tools`` contains entries for all ``@tool``
        decorators defined in the skill handlers.

        This is a best-effort helper used during DEBUG runtime sync to keep
        manifests and runtime slots aligned while iterating on workspace
        skills. It only appends missing tools with generic schemas and never
        removes or rewrites existing entries.
        """
        space_normalized = (space or "workspace").strip().lower()
        if space_normalized not in ("workspace", "dev"):
            raise ValueError("space must be 'workspace' or 'dev'")

        ctx = self.ctx
        if space_normalized == "dev":
            root = ctx.paths.dev_skills_dir()
        else:
            root = ctx.paths.skills_workspace_dir()
        root = root() if callable(root) else root
        skill_dir = Path(root) / name
        skill_yaml = skill_dir / "skill.yaml"
        handlers_main = skill_dir / "handlers" / "main.py"
        if not handlers_main.exists():
            return {"ok": False, "reason": "handlers_missing", "skill": name, "space": space_normalized}
        try:
            source = handlers_main.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "ok": False,
                "reason": "read_handlers_failed",
                "skill": name,
                "space": space_normalized,
                "error": str(exc),
            }

        try:
            tree = ast.parse(source, filename=str(handlers_main))
        except SyntaxError as exc:
            return {
                "ok": False,
                "reason": "parse_failed",
                "skill": name,
                "space": space_normalized,
                "error": str(exc),
            }

        discovered: list[tuple[str, str]] = []

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            func_name = node.name
            for dec in node.decorator_list:
                # Match @tool("name") or @tool(name="...").
                tool_name: str | None = None
                if isinstance(dec, ast.Call):
                    target = dec.func
                    is_tool = False
                    if isinstance(target, ast.Name) and target.id == "tool":
                        is_tool = True
                    elif isinstance(target, ast.Attribute) and target.attr == "tool":
                        is_tool = True
                    if not is_tool:
                        continue
                    if dec.args:
                        arg = dec.args[0]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            tool_name = arg.value
                    if not tool_name:
                        for kw in dec.keywords or []:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                tool_name = kw.value.value
                                break
                elif isinstance(dec, ast.Name) and dec.id == "tool":
                    tool_name = func_name
                if tool_name:
                    discovered.append((tool_name, func_name))

        if not discovered:
            return {"ok": True, "skill": name, "space": space_normalized, "tools_added": []}

        manifest: dict[str, Any] = {}
        if skill_yaml.exists():
            try:
                raw = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    manifest = raw
            except Exception:
                manifest = {}

        tools = manifest.get("tools") or []
        if not isinstance(tools, list):
            tools = []
        existing = {entry.get("name") for entry in tools if isinstance(entry, dict)}

        added_entries: list[dict[str, Any]] = []
        for tool_name, func_name in discovered:
            if tool_name in existing:
                continue
            entry = {
                "name": tool_name,
                "description": f"Auto-generated from handlers.main:{func_name}",
                "entry": f"handlers.main:{func_name}",
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
            }
            tools.append(entry)
            existing.add(tool_name)
            added_entries.append(entry)

        if added_entries:
            manifest["tools"] = tools
            try:
                skill_yaml.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
            except Exception:
                return {
                    "ok": False,
                    "reason": "write_skill_yaml_failed",
                    "skill": name,
                    "space": space_normalized,
                    "tools_added": [e["name"] for e in added_entries],
                }

        return {
            "ok": True,
            "skill": name,
            "space": space_normalized,
            "tools_added": [e["name"] for e in added_entries],
        }
        skill_env_path: Path | None = None
        log_path: Path

        if not version:
            raise RuntimeError("no versions installed")

        env.prepare_version(version)
        current_link = env.ensure_current_link(version)
        metadata = env.read_version_metadata(version)
        active_slot = env.read_active_slot(version)
        slot_paths = env.build_slot_paths(version, active_slot)
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        manifest_override = slot_meta.get("resolved_manifest") if isinstance(slot_meta, dict) else None
        manifest_path = Path(manifest_override or slot_paths.resolved_manifest)
        if not manifest_path.exists():
            for candidate in env.iter_slot_paths(version):
                candidate_meta = metadata.get("slots", {}).get(candidate.slot, {})
                override = candidate_meta.get("resolved_manifest") if isinstance(candidate_meta, dict) else None
                candidate_manifest = Path(override or candidate.resolved_manifest)
                if candidate_manifest.exists():
                    slot_paths = candidate
                    manifest_path = candidate_manifest
                    break

        if not manifest_path.exists():
            raise RuntimeError("no prepared slot with resolved manifest; install the skill first")

        log_path = slot_paths.logs_dir / "tests.manual.log"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}

        runtime_info = manifest.get("runtime", {})
        interpreter_value = runtime_info.get("interpreter")
        if interpreter_value:
            interpreter = Path(interpreter_value)
        python_paths.extend([p for p in runtime_info.get("python_paths", []) if p])

        source_override = manifest.get("source")
        skill_source = Path(source_override) if source_override else slot_paths.src_dir

        skill_env_raw = runtime_info.get("skill_env")
        if skill_env_raw:
            skill_env_path = Path(skill_env_raw)
        if not skill_env_path:
            skill_env_path = slot_paths.skill_env_path

        if package_root:
            python_paths.append(str(package_root))

        # Include dev/workspace convenience paths for compatibility with
        # existing skill tests that import via `skills.*` from the developer
        # workspace. This does not affect CLI test isolation which manages
        # its own PYTHONPATH.
        dev_dir = self.ctx.paths.dev_dir()
        python_paths.insert(0, str(skill_dir))
        python_paths.insert(0, str(dev_dir))

        results = run_tests(
            skill_source,
            log_path=log_path,
            interpreter=interpreter,
            python_paths=python_paths,
            skill_env_path=skill_env_path,
            skill_name=name,
            skill_version=version,
            slot_current_dir=current_link,
        )
        for test_name, result in list(results.items()):
            if result and result.status in ("error", "failed"):
                detail = f"{result.detail} (log: {log_path})" if result.detail else f"log: {log_path}"
                results[test_name] = replace(result, detail=detail)
        return results

    def uninstall(self, name: str, *, safe: bool = False, force: bool = False) -> None:
        self.caps.require("core", "skills.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid skill name")
        # если записи нет — считаем idempotent
        rec = self.reg.get(name)
        if not rec:
            return f"uninstalled: {name} (not found)"
        self.reg.unregister(name)
        root = self.ctx.paths.workspace_dir()
        # в тестах/без .git — только реестр, без git операций
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if test_mode or not (root / ".git").exists():
            suffix = " test-mode" if test_mode else ""
            return f"uninstalled: {name} (registry-only{suffix})"
        names = [r.name for r in self.reg.list()]
        prefixed = [f"skills/{n}" for n in names]
        if force:
            stash_ref = self.ctx.git.stash_push(
                str(root),
                f"adaos:auto-stash forced skill uninstall {name}",
                include_untracked=True,
            )
            _log.warning(
                "forced skill uninstall requested skill=%s repo=%s stash=%s",
                name,
                str(root),
                str(stash_ref or "-"),
            )
        elif safe:
            # Безопасный режим: проверяем отсутствие незакоммиченных изменений под управляемыми путями.
            ensure_clean(self.ctx.git, str(root), prefixed)
        self.ctx.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.ctx.git.sparse_set(str(root), prefixed, no_cone=True)
        if safe and not force:
            # В безопасном режиме обновляем workspace, чтобы поддерево навыков соответствовало удалённому репо.
            self.ctx.git.pull(str(root))
        remove_error: Exception | None = None
        try:
            remove_tree(
                str(root / "skills" / name),
                fs=self.ctx.paths.ctx.fs if hasattr(self.ctx.paths, "ctx") else get_ctx().fs,
            )
        except PermissionError as exc:
            remove_error = exc
        self.cleanup_runtime(name, purge_data=True)
        if remove_error is not None:
            raise RuntimeError(f"не удалось удалить рабочую копию навыка '{name}'. Закройте файлы под " f"путем {(root / 'skills' / name)} и повторите попытку.") from remove_error
        emit(self.bus, "skill.uninstalled", {"id": name}, "skill.mgr")
        try:
            uninstall_skill_from_capacity(name)
            try:
                from adaos.services.node_config import load_config
                from adaos.services.capacity import get_local_capacity
                from adaos.services.registry.subnet_directory import get_directory
                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_skill_capacity(conf.node_id, cap.get("skills") or [])
            except Exception:
                pass
        except Exception:
            pass

    def push(self, name: str, message: str, *, signoff: bool = False, bump: bool = True) -> str:
        self.caps.require("core", "skills.manage", "git.write", "net.git")
        root = self.ctx.paths.workspace_dir()
        try:
            from adaos.services.git.availability import get_git_availability

            av = get_git_availability(base_dir=self.ctx.settings.base_dir)
            if not av.enabled:
                raise RuntimeError("Git is disabled/unavailable on this node. Run `adaos git enable` when git is installed.")
        except ImportError:
            pass
        if not (root / ".git").exists():
            raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")

        sub = name.strip()
        subpath = f"skills/{sub}"
        self._ensure_skill_subpath_materialized(root, sub)
        version = self._bump_skill_manifest_for_push(root / "skills" / sub) if bump else None
        upsert_workspace_registry_entry(root, "skills", root / "skills" / sub)
        if version and getattr(self, "reg", None) is not None:
            try:
                self.reg.register(sub, active_version=version)
            except Exception:
                pass
        changed = sorted(
            {
                *self.ctx.git.changed_files(str(root), subpath=subpath),
                *self.ctx.git.changed_files(str(root), subpath="registry.json"),
            }
        )
        if not changed:
            return "nothing-to-push"
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        # безопасно получаем автора
        if self.settings:
            author_name = self.settings.git_author_name
            author_email = self.settings.git_author_email
        else:
            # fallback, если кто-то создаст менеджер без settings
            try:
                ctx = get_ctx()
                author_name = ctx.settings.git_author_name
                author_email = ctx.settings.git_author_email
            except Exception:
                author_name, author_email = "AdaOS Bot", "bot@adaos.local"
        msg = sanitize_message(message)
        sha = self.ctx.git.commit_subpath(
            str(root),
            subpath=[subpath, "registry.json"],
            message=msg,
            author_name=author_name,
            author_email=author_email,
            signoff=signoff,
        )
        if sha != "nothing-to-commit":
            self.ctx.git.push(str(root))
        return sha

    def _ensure_skill_subpath_materialized(self, root: Path, name: str) -> None:
        skill_dir = root / "skills" / name
        has_manifest = any((skill_dir / item).exists() for item in _SKILL_MANIFEST_NAMES)
        try:
            self.ctx.git.sparse_add(str(root), f"skills/{name}")
        except Exception:
            return
        if has_manifest:
            return
        try:
            self.ctx.git.pull(str(root))
        except Exception:
            pass
        try:
            wait_for_materialized(skill_dir, files=_SKILL_MANIFEST_NAMES, attempts=5, delay=0.1)
        except FileNotFoundError:
            if not skill_dir.exists():
                raise FileNotFoundError(
                    f"skill '{name}' is not materialized in workspace sparse checkout"
                ) from None

    def _bump_skill_manifest_for_push(self, skill_dir: Path) -> str | None:
        skill_yaml = skill_dir / "skill.yaml"
        if not skill_yaml.exists():
            return None
        try:
            payload = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        existing = payload.get("version")
        existing_version = existing if isinstance(existing, str) and existing.strip() else None
        effective_bump = effective_skill_bump(
            payload,
            "patch",
            has_data_migration_file=(skill_dir / RESERVED_DATA_MIGRATION_FILE).is_file(),
        )
        payload["version"] = bump_version(existing_version, bump_index(effective_bump))
        payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        skill_yaml.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return str(payload.get("version") or "")

    # ------------------------------------------------------------------
    # Runtime lifecycle helpers
    # ------------------------------------------------------------------
    def prepare_runtime(
        self,
        name: str,
        *,
        path: Path | None = None,
        version_override: str | None = None,
        run_tests: bool = False,
        preferred_slot: str | None = None,
    ) -> RuntimeInstallResult:
        skills_root = self.ctx.paths.skills_dir()
        skill_dir = Path(path).resolve() if path is not None else (skills_root / name)
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        manifest = self._load_manifest(skill_dir)
        version = version_override or str(manifest.get("version") or "0.0.0")

        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=name)
        env.prepare_version(version)

        slot_name = preferred_slot or env.select_inactive_slot(version)
        slot = env.build_slot_paths(version, slot_name)

        # Ensure clean slot state before preparing runtime
        env.cleanup_slot(version, slot_name)
        env.prepare_version(version)
        slot = env.build_slot_paths(version, slot_name)

        try:
            staged_dir = self._stage_skill_sources(skill_dir, slot)
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise

        try:
            interpreter, python_paths = self._prepare_runtime_environment(
                env=env,
                slot=slot,
                manifest=manifest,
                skill_dir=staged_dir,
            )
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise
        defaults = self._policy_defaults()
        policy_overrides = self._policy_overrides()

        resolved = self._enrich_manifest(
            manifest=manifest,
            slot=slot,
            interpreter=interpreter,
            python_paths=python_paths,
            defaults=defaults,
            policy_overrides=policy_overrides,
            skill_dir=staged_dir,
        )
        self._write_resolved_manifest(slot, resolved)
        lifecycle = self._prepared_lifecycle_state()
        data_migration = self._prepare_bucket_data(
            env=env,
            slot=slot,
            resolved_manifest=resolved,
            skill_dir=staged_dir,
            python_paths=python_paths,
        )
        lifecycle["migrate"] = {"ok": True, "mode": str(data_migration.get("mode") or "shared")}

        tests: Dict[str, TestResult] = {}
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        package_root = None
        if package_dir:
            package_root = Path(package_dir).resolve().parent
        if run_tests:
            log_file = slot.logs_dir / "tests.log"
            extra_paths = list(python_paths)
            if package_root:
                extra_paths.append(str(package_root))
            tests = run_tests(
                staged_dir,
                log_path=log_file,
                interpreter=interpreter,
                python_paths=extra_paths,
                skill_env_path=slot.skill_env_path,
                skill_name=name,
                skill_version=version,
                slot_current_dir=slot.root,
            )
            if any(result.status != "passed" for result in tests.values()):
                env.cleanup_slot(version, slot_name)
                raise RuntimeError("skill tests failed")
        lifecycle["healthcheck"] = {
            "ok": True,
            "stage": "prepare",
            "tests": {name: result.status for name, result in tests.items()},
        }

        metadata = env.read_version_metadata(version)
        slots_meta = metadata.setdefault("slots", {})
        slots_meta[slot_name] = {
            "version": version,
            "runtime_bucket": env.runtime_bucket(version),
            "resolved_manifest": str(slot.resolved_manifest),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "tests": {name: result.status for name, result in tests.items()},
            "data_migration": dict(data_migration),
            "lifecycle": dict(lifecycle),
        }
        metadata["version"] = version
        metadata["runtime_bucket"] = env.runtime_bucket(version)
        if data_migration.get("bucket_migration"):
            metadata["bucket_data_migration"] = dict(data_migration)
        history = metadata.setdefault("history", {})
        history["last_install_slot"] = slot_name
        history["last_install_version"] = version
        history["last_install_at"] = datetime.now(timezone.utc).isoformat()
        history["last_default_tool"] = resolved.get("default_tool")
        env.write_version_metadata(version, metadata)

        return RuntimeInstallResult(
            name=name,
            version=version,
            slot=slot_name,
            resolved_manifest=slot.resolved_manifest,
            tests=tests,
            data_migration=dict(data_migration),
            lifecycle=dict(lifecycle),
        )

    def activate_runtime(self, name: str, *, version: str | None = None, slot: str | None = None) -> str:
        env = self._runtime_env(name)
        source_path: Path | None = None
        previous_active_version = env.resolve_active_version()
        previous_active_slot = env.read_active_slot(previous_active_version) if previous_active_version else None
        previous_deactivation = env.read_deactivation()
        source_version = ""
        try:
            candidate, _source_kind = self._resolve_runtime_update_source(name, space="workspace")
            if candidate.exists():
                source_path = candidate
                try:
                    source_manifest = self._load_manifest(source_path)
                except FileNotFoundError:
                    source_manifest = {}
                source_version = str(source_manifest.get("version") or "").strip()
        except Exception:
            source_path = None
            source_version = ""
        prepared_version = self._latest_prepared_version(env)
        if version:
            target_version = version
        elif source_version:
            target_version = source_version
        else:
            target_version = prepared_version or previous_active_version
        if not target_version:
            if source_path is None:
                raise RuntimeError("no installed versions")
            try:
                manifest = self._load_manifest(source_path)
            except FileNotFoundError:
                manifest = {}
            target_version = str(manifest.get("version") or "0.0.0")
        env.prepare_version(target_version)
        metadata = env.read_version_metadata(target_version)
        target_slot = slot or self._preferred_activation_slot(env, target_version, metadata)
        slot_paths = env.build_slot_paths(target_version, target_slot)
        slot_meta = metadata.get("slots", {}).get(target_slot, {})
        manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
        target_manifest: dict[str, Any] = {}
        slot_version = str(slot_meta.get("version") or "").strip()
        needs_prepare = not manifest_path.exists()
        if (
            not needs_prepare
            and source_path is not None
            and source_version
            and source_version == str(target_version or "").strip()
            and slot_version != str(target_version or "").strip()
        ):
            needs_prepare = True
        if needs_prepare:
            if source_path is None:
                raise RuntimeError(
                    f"slot {target_slot} of version {target_version} is not prepared; "
                    f"run 'adaos skill install {name} --slot={target_slot}' first"
                )
            self.prepare_runtime(
                name,
                path=source_path,
                version_override=target_version,
                run_tests=False,
                preferred_slot=target_slot,
            )
            metadata = env.read_version_metadata(target_version)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} of version {target_version} is not prepared")
        target_manifest = self._read_json_dict(manifest_path)
        lifecycle = self._slot_lifecycle_state(metadata=metadata, slot=target_slot)
        persist_state = self._invoke_persist_before_switch(
            env=env,
            name=name,
            target_version=target_version,
            metadata=metadata,
        )
        lifecycle["persist"] = persist_state
        self._smoke_import(env=env, name=name, version=target_version, slot=target_slot)
        env.set_active_slot(target_version, target_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
        env.record_active_selection(
            target_version,
            target_slot,
            previous_version=previous_active_version,
            previous_slot=previous_active_slot,
        )
        env.clear_deactivation()
        try:
            after_activate = self._invoke_slot_lifecycle_hook(
                env=env,
                slot=slot_paths,
                resolved_manifest=target_manifest,
                hook_key="after_activate",
                payload={
                    "skill": name,
                    "version": target_version,
                    "slot": target_slot,
                    "state": "active",
                },
            )
            lifecycle["after_activate"] = after_activate
            rehydrate = self._invoke_slot_lifecycle_hook(
                env=env,
                slot=slot_paths,
                resolved_manifest=target_manifest,
                hook_key="rehydrate",
                payload={
                    "skill": name,
                    "version": target_version,
                    "slot": target_slot,
                    "state": "active",
                },
            )
            lifecycle["rehydrate"] = rehydrate
            lifecycle["healthcheck"] = {"ok": True, "stage": "activate"}
        except Exception as exc:
            lifecycle["healthcheck"] = {
                "ok": False,
                "stage": "activate",
                "error": str(exc),
            }
            shutdown_results = self._invoke_shutdown_hooks(
                env=env,
                slot=slot_paths,
                resolved_manifest=target_manifest,
                payload={
                    "skill": name,
                    "version": target_version,
                    "slot": target_slot,
                    "reason": "activation_rehydrate_failed",
                    "state": "deactivating",
                },
            )
            lifecycle.update(shutdown_results)
            lifecycle["rollback"] = self._restore_runtime_selection(
                env=env,
                previous_active_version=previous_active_version,
                previous_active_slot=previous_active_slot,
                previous_deactivation=previous_deactivation,
            )
            metadata.setdefault("slots", {}).setdefault(target_slot, {})["lifecycle"] = dict(lifecycle)
            history = metadata.setdefault("history", {})
            history["last_activation_error"] = str(exc)
            history["last_activation_error_at"] = datetime.now(timezone.utc).isoformat()
            env.write_version_metadata(target_version, metadata)
            raise RuntimeError(f"activation rehydrate failed: {exc}") from exc
        history = metadata.setdefault("history", {})
        history["last_active_slot"] = target_slot
        history["last_active_version"] = target_version
        history["last_active_at"] = datetime.now(timezone.utc).isoformat()
        history["last_activation_error"] = ""
        history["last_activation_error_at"] = ""
        target_slot_meta = metadata.setdefault("slots", {}).setdefault(target_slot, {})
        target_slot_meta.setdefault("version", target_version)
        target_slot_meta.setdefault("runtime_bucket", env.runtime_bucket(target_version))
        target_slot_meta["lifecycle"] = dict(lifecycle)
        env.write_version_metadata(target_version, metadata)
        self._prune_runtime_history(env=env, current_version=target_version, previous_version=previous_active_version)
        try:
            install_skill_in_capacity(name, target_version, active=True)
            try:
                from adaos.services.node_config import load_config
                from adaos.services.capacity import get_local_capacity
                from adaos.services.registry.subnet_directory import get_directory
                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_skill_capacity(conf.node_id, cap.get("skills") or [])
            except Exception:
                pass
        except Exception:
            pass
        return target_slot

    def rollback_runtime(self, name: str) -> str:
        env = self._runtime_env(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        current_slot = env.read_active_slot(version)
        metadata = env.read_version_metadata(version)
        current_paths = env.build_slot_paths(version, current_slot)
        current_manifest = self._read_json_dict(current_paths.resolved_manifest)
        lifecycle = self._slot_lifecycle_state(metadata=metadata, slot=current_slot)
        lifecycle.update(
            self._invoke_shutdown_hooks(
                env=env,
                slot=current_paths,
                resolved_manifest=current_manifest,
                payload={
                    "skill": name,
                    "version": version,
                    "slot": current_slot,
                    "reason": "rollback",
                    "state": "deactivating",
                },
            )
        )
        metadata.setdefault("slots", {}).setdefault(current_slot, {})["lifecycle"] = dict(lifecycle)
        env.write_version_metadata(version, metadata)
        previous_selection = env.read_runtime_selection(previous=True)
        restored_version = str(previous_selection.get("version") or "").strip()
        restored_slot = str(previous_selection.get("slot") or "").strip().upper()
        if restored_version and restored_slot in {"A", "B"} and (
            restored_version != version or restored_slot != current_slot
        ):
            env.prepare_version(restored_version)
            env.set_active_slot(restored_version, restored_slot)
        else:
            restored_slot = env.rollback_slot(version)
            restored_version = self._slot_version(
                env=env,
                metadata=metadata,
                slot=restored_slot,
                fallback=version,
            )
        env.active_version_marker().write_text(restored_version, encoding="utf-8")
        env.record_active_selection(
            restored_version,
            restored_slot,
            previous_version=version,
            previous_slot=current_slot,
        )
        lifecycle["rollback"] = {
            "ok": True,
            "skipped": False,
            "restored_active_version": restored_version,
            "restored_active_slot": restored_slot,
        }
        restored_metadata = metadata if restored_version == version else env.read_version_metadata(restored_version)
        history = restored_metadata.setdefault("history", {})
        history["last_active_slot"] = restored_slot
        history["last_active_version"] = restored_version
        history["last_rollback_at"] = datetime.now(timezone.utc).isoformat()
        metadata.setdefault("slots", {}).setdefault(current_slot, {})["lifecycle"] = dict(lifecycle)
        if restored_version != version:
            env.write_version_metadata(version, metadata)
        env.write_version_metadata(restored_version, restored_metadata)
        try:
            install_skill_in_capacity(name, restored_version, active=True)
        except Exception:
            pass
        return restored_slot

    def dev_rollback_runtime(self, name: str) -> str:
        env = self._runtime_env_dev(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        metadata = env.read_version_metadata(version)
        current_slot = env.read_active_slot(version)
        previous_selection = env.read_runtime_selection(previous=True)
        restored_version = str(previous_selection.get("version") or "").strip()
        restored_slot = str(previous_selection.get("slot") or "").strip().upper()
        if restored_version and restored_slot in {"A", "B"} and (
            restored_version != version or restored_slot != current_slot
        ):
            env.prepare_version(restored_version)
            env.set_active_slot(restored_version, restored_slot)
        else:
            restored_slot = env.rollback_slot(version)
            restored_version = self._slot_version(
                env=env,
                metadata=metadata,
                slot=restored_slot,
                fallback=version,
            )
        env.active_version_marker().write_text(restored_version, encoding="utf-8")
        env.record_active_selection(
            restored_version,
            restored_slot,
            previous_version=version,
            previous_slot=current_slot,
        )
        restored_metadata = env.read_version_metadata(restored_version)
        history = restored_metadata.setdefault("history", {})
        history["last_active_slot"] = restored_slot
        history["last_active_version"] = restored_version
        history["last_rollback_at"] = datetime.now(timezone.utc).isoformat()
        env.write_version_metadata(restored_version, restored_metadata)
        return restored_slot

    def activate_for_space(
        self,
        name: str,
        *,
        space: str = "default",
        webspace_id: str | None = None,
        version: str | None = None,
        slot: str | None = None,
    ) -> str:
        """
        Convenience helper that routes activation to the appropriate runtime
        (default vs dev) and emits a unified skills.activated event.
        """
        if space == "dev":
            target = self.activate_dev_runtime(name, version=version, slot=slot)
        else:
            target = self.activate_runtime(name, version=version, slot=slot)
        bus_webspace = webspace_id or _default_webspace_id()
        if self.bus:
            payload: Dict[str, Any] = {"skill_name": name, "space": space, "webspace_id": bus_webspace}
            emit(self.bus, "skills.activated", payload, "skill.mgr")
        return target

    def rollback_for_space(self, name: str, *, space: str = "default", webspace_id: str | None = None) -> str:
        """
        Roll back the active runtime slot for the requested space and emit
        a skills.rolledback event for observers.
        """
        if space == "dev":
            target = self.dev_rollback_runtime(name)
        else:
            target = self.rollback_runtime(name)
        bus_webspace = webspace_id or _default_webspace_id()
        if self.bus:
            payload: Dict[str, Any] = {"skill_name": name, "space": space, "webspace_id": bus_webspace}
            emit(self.bus, "skills.rolledback", payload, "skill.mgr")
        return target

    def deactivate_runtime(
        self,
        name: str,
        *,
        reason: str = "post_commit_checks_failed",
        failure_kind: str = "",
        failed_stage: str = "",
        source: str = "",
        committed_core_switch: bool | None = None,
    ) -> dict[str, Any]:
        env = self._runtime_env(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        active_slot = env.read_active_slot(version)
        metadata = env.read_version_metadata(version)
        active_paths = env.build_slot_paths(version, active_slot)
        active_manifest = self._read_json_dict(active_paths.resolved_manifest)
        lifecycle = self._slot_lifecycle_state(metadata=metadata, slot=active_slot)
        lifecycle.update(
            self._invoke_shutdown_hooks(
                env=env,
                slot=active_paths,
                resolved_manifest=active_manifest,
                payload={
                    "skill": name,
                    "version": version,
                    "slot": active_slot,
                    "reason": str(reason or "post_commit_checks_failed"),
                    "failure_kind": str(failure_kind or "").strip(),
                    "failed_stage": str(failed_stage or "").strip(),
                    "source": str(source or "").strip(),
                    "committed_core_switch": bool(committed_core_switch),
                    "state": "deactivating",
                },
            )
        )
        metadata.setdefault("slots", {}).setdefault(active_slot, {})["lifecycle"] = dict(lifecycle)
        env.write_version_metadata(version, metadata)
        deactivation_reason = str(reason or "post_commit_checks_failed")
        deactivation_failure_kind = str(failure_kind or "").strip()
        deactivation_failed_stage = str(failed_stage or "").strip()
        deactivation_source = str(source or "").strip()
        payload = {
            "name": name,
            "version": version,
            "slot": active_slot,
            "reason": deactivation_reason,
            "deactivated": True,
            "failure_kind": deactivation_failure_kind,
            "failed_stage": deactivation_failed_stage,
            "source": deactivation_source,
            "committed_core_switch": bool(committed_core_switch),
        }
        env.write_deactivation(payload)
        try:
            install_skill_in_capacity(name, version, active=False)
        except Exception:
            pass
        if self.bus:
            emit(self.bus, "skills.deactivated", dict(payload), "skill.mgr")
        return payload

    def deactivate_for_space(
        self,
        name: str,
        *,
        space: str = "default",
        webspace_id: str | None = None,
        reason: str = "post_commit_checks_failed",
        failure_kind: str = "",
        failed_stage: str = "",
        source: str = "",
        committed_core_switch: bool | None = None,
    ) -> dict[str, Any]:
        if space == "dev":
            return self.deactivate_dev_runtime(
                name,
                reason=reason,
                failure_kind=failure_kind,
                failed_stage=failed_stage,
                source=source,
                committed_core_switch=committed_core_switch,
            )
        else:
            return self.deactivate_runtime(
                name,
                reason=reason,
                failure_kind=failure_kind,
                failed_stage=failed_stage,
                source=source,
                committed_core_switch=committed_core_switch,
            )

    def runtime_status(self, name: str) -> Dict[str, Any]:
        env = self._runtime_env(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no versions installed")
        version_root = env.version_root(version)
        active_marker = version_root / "active"
        active_slot = "A"
        if active_marker.exists():
            value = active_marker.read_text(encoding="utf-8").strip().upper()
            if value in {"A", "B"}:
                active_slot = value
        metadata_path = version_root / "meta.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        except json.JSONDecodeError:
            metadata = {}
        slot_root = version_root / "slots" / active_slot
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        resolved_path = Path(slot_meta.get("resolved_manifest") or (slot_root / "resolved.manifest.json"))
        ready = resolved_path.exists()
        history = metadata.get("history", {})
        deactivation = env.read_deactivation()
        deactivated = bool(deactivation.get("deactivated"))
        state: Dict[str, Any] = {
            "name": name,
            "version": version,
            "runtime_bucket": env.runtime_bucket(version),
            "active_slot": active_slot,
            "resolved_manifest": str(resolved_path),
            "ready": ready,
            "active": not deactivated,
            "deactivated": deactivated,
            "deactivation": deactivation,
            "tests": slot_meta.get("tests", {}),
            "history": history,
            "data_migration": slot_meta.get("data_migration", {}),
            "lifecycle": slot_meta.get("lifecycle", {}),
        }
        if not ready:
            state["pending_slot"] = history.get("last_install_slot")
            state["pending_version"] = history.get("last_install_version")
            state["default_tool"] = history.get("last_default_tool")
        else:
            try:
                manifest = json.loads(resolved_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
            state["default_tool"] = manifest.get("default_tool")
        return state

    def dev_runtime_status(self, name: str) -> Dict[str, Any]:
        env = self._runtime_env_dev(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no versions installed")
        version_root = env.version_root(version)
        active_marker = version_root / "active"
        active_slot = "A"
        if active_marker.exists():
            value = active_marker.read_text(encoding="utf-8").strip().upper()
            if value in {"A", "B"}:
                active_slot = value
        metadata_path = version_root / "meta.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        except json.JSONDecodeError:
            metadata = {}
        slot_root = version_root / "slots" / active_slot
        slot_meta = metadata.get("slots", {}).get(active_slot, {})
        resolved_path = Path(slot_meta.get("resolved_manifest") or (slot_root / "resolved.manifest.json"))
        ready = resolved_path.exists()
        history = metadata.get("history", {})
        deactivation = env.read_deactivation()
        deactivated = bool(deactivation.get("deactivated"))
        state: Dict[str, Any] = {
            "name": name,
            "version": version,
            "runtime_bucket": env.runtime_bucket(version),
            "active_slot": active_slot,
            "resolved_manifest": str(resolved_path),
            "ready": ready,
            "active": not deactivated,
            "deactivated": deactivated,
            "deactivation": deactivation,
            "tests": slot_meta.get("tests", {}),
            "history": history,
            "data_migration": slot_meta.get("data_migration", {}),
            "lifecycle": slot_meta.get("lifecycle", {}),
        }
        if not ready:
            state["pending_slot"] = history.get("last_install_slot")
            state["pending_version"] = history.get("last_install_version")
            state["default_tool"] = history.get("last_default_tool")
        else:
            try:
                manifest = json.loads(resolved_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
            state["default_tool"] = manifest.get("default_tool")
        return state

    def deactivate_dev_runtime(
        self,
        name: str,
        *,
        reason: str = "post_commit_checks_failed",
        failure_kind: str = "",
        failed_stage: str = "",
        source: str = "",
        committed_core_switch: bool | None = None,
    ) -> dict[str, Any]:
        env = self._runtime_env_dev(name)
        version = env.resolve_active_version()
        if not version:
            raise RuntimeError("no active version")
        env.prepare_version(version)
        active_slot = env.read_active_slot(version)
        payload = {
            "name": name,
            "version": version,
            "slot": active_slot,
            "reason": str(reason or "post_commit_checks_failed"),
            "deactivated": True,
            "failure_kind": str(failure_kind or "").strip(),
            "failed_stage": str(failed_stage or "").strip(),
            "source": str(source or "").strip(),
            "committed_core_switch": bool(committed_core_switch),
            "dev": True,
        }
        env.write_deactivation(payload)
        try:
            install_skill_in_capacity(name, version, active=False, dev=True)
        except Exception:
            pass
        if self.bus:
            emit(self.bus, "skills.deactivated", dict(payload), "skill.mgr")
        return payload

    def cleanup_runtime(self, name: str, *, purge_data: bool = False) -> None:
        env = self._runtime_env(name)
        if env.runtime_root.exists():
            for child in list(env.runtime_root.iterdir()):
                if not child.is_dir() or not env.is_runtime_bucket_name(child.name):
                    continue
                if purge_data:
                    self._remove_tree(child)
                    continue
                for entry in list(child.iterdir()):
                    if entry.name == "data":
                        continue
                    if entry.is_dir():
                        self._remove_tree(entry)
                    else:
                        try:
                            entry.unlink()
                        except FileNotFoundError:
                            pass
        marker = env.active_version_marker()
        if marker.exists():
            marker.unlink()
        env.clear_runtime_selection()
        runtime_root = env.runtime_root
        if runtime_root.exists():
            try:
                runtime_root.rmdir()
            except OSError:
                pass

    def gc_runtime(self, name: str | None = None) -> Dict[str, Iterable[str]]:
        skills_root = self.ctx.paths.skills_dir()
        targets = [name] if name else [p.name for p in (skills_root / ".runtime").glob("*") if p.is_dir()]
        cleaned: Dict[str, Iterable[str]] = {}
        for skill in targets:
            env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill)
            active_version = env.resolve_active_version()
            active_bucket = env.runtime_bucket(active_version) if active_version else None
            removed: list[str] = []
            if not env.runtime_root.exists():
                cleaned[skill] = removed
                continue
            for child in list(env.runtime_root.iterdir()):
                if not child.is_dir() or not env.is_runtime_bucket_name(child.name):
                    continue
                if active_bucket and child.name == active_bucket:
                    continue
                self._remove_tree(child)
                removed.append(child.name)
            cleaned[skill] = removed
        return cleaned

    def doctor_runtime(self, name: str) -> Dict[str, Any]:
        status = self.runtime_status(name)
        ctx = self.ctx
        base = ctx.paths.skills_dir()
        return {
            "skill_root": str((base / name).resolve()),
            "runtime_root": str((base / ".runtime" / name).resolve()),
            "active_slot": status["active_slot"],
            "resolved_manifest": status["resolved_manifest"],
        }

    def setup_skill(self, name: str) -> Any:
        """Run the optional setup tool for a skill."""

        status = self.runtime_status(name)
        if not status.get("ready", True):
            pending_version = status.get("pending_version") or status.get("version")
            raise RuntimeError(f"skill '{name}' version {pending_version or '<unknown>'} is not activated. " "Run 'adaos skill activate' before setup.")

        manifest_path = Path(status["resolved_manifest"])
        if not manifest_path.exists():
            raise RuntimeError("skill runtime is not prepared; install and activate the skill first")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = manifest.get("tools") or {}
        if "setup" not in tools:
            raise RuntimeError("setup not supported for this skill")

        return self.run_tool(
            name,
            "setup",
            {},
            allow_inactive=False,
        )

    def dev_setup_skill(self, name: str) -> Any:
        """Run the optional setup tool for a DEV skill."""

        status = self.dev_runtime_status(name)
        if not status.get("ready", True):
            pending_version = status.get("pending_version") or status.get("version")
            raise RuntimeError(f"skill '{name}' version {pending_version or '<unknown>'} is not activated. Run 'adaos dev skill activate' before setup.")

        manifest_path = Path(status["resolved_manifest"])
        if not manifest_path.exists():
            raise RuntimeError("skill runtime is not prepared; install and activate the skill first")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = manifest.get("tools") or {}
        if "setup" not in tools:
            raise RuntimeError("setup not supported for this skill")

        return self.run_dev_tool(
            name,
            "setup",
            {},
            allow_inactive=False,
        )

    def run_tool(
        self,
        name: str,
        tool: str | None,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        allow_inactive: bool = False,
        slot: str | None = None,
    ) -> Any:
        status = self.runtime_status(name)
        env = self._runtime_env(name)
        version = status.get("version")
        active_slot = status.get("active_slot")
        manifest_path = Path(status["resolved_manifest"])
        slot_name = active_slot

        if not status.get("ready", True):
            target_slot = slot or status.get("pending_slot")
            target_version = status.get("pending_version") or version
            if not allow_inactive or not target_slot or not target_version:
                raise RuntimeError(
                    f"skill '{name}' version {status.get('pending_version') or status.get('version')} is not activated. "
                    f"Activate slot {target_slot or status.get('active_slot')} and retry."
                )
            env.prepare_version(target_version)
            metadata = env.read_version_metadata(target_version)
            slot_paths = env.build_slot_paths(target_version, target_slot)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} for version {target_version} is not prepared")
            version = target_version
            slot_name = target_slot
        elif slot and slot != active_slot:
            env.prepare_version(version)
            metadata = env.read_version_metadata(version)
            slot_paths = env.build_slot_paths(version, slot)
            slot_meta = metadata.get("slots", {}).get(slot, {})
            candidate = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not candidate.exists():
                raise RuntimeError(f"slot {slot} for version {version} is not prepared")
            manifest_path = candidate
            slot_name = slot
        if bool(status.get("deactivated")):
            reason = str((status.get("deactivation") or {}).get("reason") or "deactivated").strip()
            raise RuntimeError(f"skill '{name}' is deactivated: {reason}")

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or {}
        if tool:
            target_tool = tool
        else:
            target_tool = data.get("default_tool")
        if not target_tool:
            raise KeyError("tool name not provided and no default tool defined")
        tool_spec = tools.get(target_tool)
        if not tool_spec:
            available = ", ".join(sorted(tools)) or "<none>"
            raise KeyError(f"tool '{target_tool}' not found (available: {available})")

        module = tool_spec.get("module")
        attr = tool_spec.get("callable") or target_tool
        skill_dir = Path(data.get("source") or (self.ctx.paths.skills_dir() / name))
        slot_name = data.get("slot") or slot_name
        slot = env.build_slot_paths(version or data.get("version"), slot_name)
        runtime_info = data.get("runtime", {})
        extra_paths = [Path(p) for p in runtime_info.get("python_paths", []) if p]
        skill_env_path = Path(runtime_info.get("skill_env") or slot.skill_env_path)
        skill_memory_path = Path(runtime_info.get("skill_memory") or skill_env_path)
        slot_data_root = Path(getattr(slot, "data_root", env.data_root()))

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH")
        prev_secrets = ctx.secrets
        execution_timeout = timeout or tool_spec.get("timeout_seconds")
        admission = _admit_skill_tool_yjs_work(name, target_tool, payload, tool_spec)
        if not bool(admission.get("allowed", True)):
            event = _skill_quarantine_event(name=name, tool=target_tool, payload=payload, admission=admission)
            _append_skill_quarantine_log(skill_memory_path, event)
            hook_status = _invoke_skill_quarantine_hook(
                ctx=ctx,
                name=name,
                tools=tools,
                blocked_tool=target_tool,
                skill_dir=skill_dir,
                skill_env_path=skill_env_path,
                skill_memory_path=skill_memory_path,
                secrets_path=slot_data_root / "files" / "secrets.json",
                extra_paths=extra_paths,
                event=event,
                admission=admission,
            )
            if bool(hook_status.get("called")):
                _append_skill_quarantine_log(
                    skill_memory_path,
                    {
                        "event": "skill.quarantine_hook",
                        "schema": "adaos.skill_quarantine_hook.v1",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "updated_at": time.time(),
                        "skill": name,
                        "blocked_tool": target_tool,
                        "hook": hook_status.get("hook"),
                        "ok": bool(hook_status.get("ok")),
                        "error": hook_status.get("error"),
                    },
                )
            denied = _skill_tool_yjs_denied_result(
                name=name,
                tool=target_tool,
                payload=payload,
                admission=admission,
            )
            denied["quarantine_hook"] = {
                key: hook_status.get(key)
                for key in ("called", "ok", "hook", "reason", "error")
                if key in hook_status
            }
            return denied
        ctx.secrets = SecretsService(SkillSecretsBackend(slot_data_root / "files" / "secrets.json"), ctx.caps)

        def _call_tool() -> Any:
            with use_ctx(ctx):
                return execute_tool(
                    skill_dir,
                    module=module,
                    attr=attr,
                    payload=payload,
                    extra_paths=extra_paths,
                )

        try:
            if not ctx.skill_ctx.set(name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{name}'")
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(skill_env_path)
            os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(skill_memory_path)

            if execution_timeout:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
                from contextvars import copy_context

                with ThreadPoolExecutor(max_workers=1) as pool:
                    ctxvars = copy_context()
                    future = pool.submit(lambda: ctxvars.run(_call_tool))
                    try:
                        result = future.result(timeout=execution_timeout)
                    except FuturesTimeoutError as exc:
                        future.cancel()
                        raise TimeoutError(f"tool '{target_tool}' timed out after {execution_timeout} seconds") from exc
            else:
                result = _call_tool()
        finally:
            ctx.secrets = prev_secrets
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env
            if prev_memory is None:
                os.environ.pop("ADAOS_SKILL_MEMORY_PATH", None)
            else:
                os.environ["ADAOS_SKILL_MEMORY_PATH"] = prev_memory

        self._persist_skill_env(env, slot)
        return result

    def run_dev_tool(
        self,
        name: str,
        tool: str | None,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        allow_inactive: bool = False,
        slot: str | None = None,
    ) -> Any:
        status = self.dev_runtime_status(name)
        env = self._runtime_env_dev(name)
        version = status.get("version")
        active_slot = status.get("active_slot")
        manifest_path = Path(status["resolved_manifest"])
        slot_name = active_slot

        if not status.get("ready", True):
            target_slot = slot or status.get("pending_slot")
            target_version = status.get("pending_version") or version
            if not allow_inactive or not target_slot or not target_version:
                raise RuntimeError(
                    f"skill '{name}' version {status.get('pending_version') or status.get('version')} is not activated. "
                    f"Activate slot {target_slot or status.get('active_slot')} and retry."
                )
            env.prepare_version(target_version)
            metadata = env.read_version_metadata(target_version)
            slot_paths = env.build_slot_paths(target_version, target_slot)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} for version {target_version} is not prepared")
            version = target_version
            slot_name = target_slot
        elif slot and slot != active_slot:
            env.prepare_version(version)
            metadata = env.read_version_metadata(version)
            slot_paths = env.build_slot_paths(version, slot)
            slot_meta = metadata.get("slots", {}).get(slot, {})
            candidate = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not candidate.exists():
                raise RuntimeError(f"slot {slot} for version {version} is not prepared")
            manifest_path = candidate
            slot_name = slot
        if bool(status.get("deactivated")):
            reason = str((status.get("deactivation") or {}).get("reason") or "deactivated").strip()
            raise RuntimeError(f"skill '{name}' is deactivated: {reason}")

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or {}
        if tool:
            target_tool = tool
        else:
            target_tool = data.get("default_tool")
        if not target_tool:
            raise KeyError("tool name not provided and no default tool defined")
        tool_spec = tools.get(target_tool)
        if not tool_spec:
            available = ", ".join(sorted(tools)) or "<none>"
            raise KeyError(f"tool '{target_tool}' not found (available: {available})")

        module = tool_spec.get("module")
        attr = tool_spec.get("callable") or target_tool
        skill_dir = Path(data.get("source") or (self.ctx.paths.dev_skills_dir() / name))
        slot_name = data.get("slot") or slot_name
        slot = env.build_slot_paths(version or data.get("version"), slot_name)
        runtime_info = data.get("runtime", {})
        extra_paths = [Path(p) for p in runtime_info.get("python_paths", []) if p]
        skill_env_path = Path(runtime_info.get("skill_env") or slot.skill_env_path)
        skill_memory_path = Path(runtime_info.get("skill_memory") or skill_env_path)
        slot_data_root = Path(getattr(slot, "data_root", env.data_root()))

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH")
        prev_secrets = ctx.secrets
        execution_timeout = timeout or tool_spec.get("timeout_seconds")
        admission = _admit_skill_tool_yjs_work(name, target_tool, payload, tool_spec)
        if not bool(admission.get("allowed", True)):
            event = _skill_quarantine_event(name=name, tool=target_tool, payload=payload, admission=admission)
            _append_skill_quarantine_log(skill_memory_path, event)
            hook_status = _invoke_skill_quarantine_hook(
                ctx=ctx,
                name=name,
                tools=tools,
                blocked_tool=target_tool,
                skill_dir=skill_dir,
                skill_env_path=skill_env_path,
                skill_memory_path=skill_memory_path,
                secrets_path=slot_data_root / "files" / "secrets.json",
                extra_paths=extra_paths,
                event=event,
                admission=admission,
            )
            if bool(hook_status.get("called")):
                _append_skill_quarantine_log(
                    skill_memory_path,
                    {
                        "event": "skill.quarantine_hook",
                        "schema": "adaos.skill_quarantine_hook.v1",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "updated_at": time.time(),
                        "skill": name,
                        "blocked_tool": target_tool,
                        "hook": hook_status.get("hook"),
                        "ok": bool(hook_status.get("ok")),
                        "error": hook_status.get("error"),
                    },
                )
            denied = _skill_tool_yjs_denied_result(
                name=name,
                tool=target_tool,
                payload=payload,
                admission=admission,
            )
            denied["quarantine_hook"] = {
                key: hook_status.get(key)
                for key in ("called", "ok", "hook", "reason", "error")
                if key in hook_status
            }
            return denied
        ctx.secrets = SecretsService(SkillSecretsBackend(slot_data_root / "files" / "secrets.json"), ctx.caps)

        def _call_tool() -> Any:
            with use_ctx(ctx):
                return execute_tool(
                    skill_dir,
                    module=module,
                    attr=attr,
                    payload=payload,
                    extra_paths=extra_paths,
                )

        try:
            if not ctx.skill_ctx.set(name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{name}'")
        except Exception:
            pass
        try:
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(skill_env_path)
            os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(skill_memory_path)
            if execution_timeout:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
                from contextvars import copy_context

                with ThreadPoolExecutor(max_workers=1) as pool:
                    ctxvars = copy_context()
                    future = pool.submit(lambda: ctxvars.run(_call_tool))
                    try:
                        result = future.result(timeout=execution_timeout)
                    except FuturesTimeoutError as exc:
                        future.cancel()
                        raise TimeoutError(f"tool '{target_tool}' timed out after {execution_timeout} seconds") from exc
            else:
                result = _call_tool()
        finally:
            ctx.secrets = prev_secrets
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env
            if prev_memory is None:
                os.environ.pop("ADAOS_SKILL_MEMORY_PATH", None)
            else:
                os.environ["ADAOS_SKILL_MEMORY_PATH"] = prev_memory

        self._persist_skill_env(env, slot)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _runtime_env(self, name: str) -> SkillRuntimeEnvironment:
        return SkillRuntimeEnvironment(
            skills_root=self.ctx.paths.skills_dir(),
            skill_name=name,
        )

    def _runtime_env_dev(self, name: str) -> SkillRuntimeEnvironment:
        return SkillRuntimeEnvironment(
            skills_root=self.ctx.paths.dev_skills_dir(),
            skill_name=name,
        )

    def _load_manifest(self, skill_dir: Path) -> Dict[str, Any]:
        candidates = ["resolved.manifest.json", "skill.yaml", "manifest.yaml", "manifest.json", "skill.json"]
        for name in candidates:
            path = skill_dir / name
            if not path.exists():
                continue
            if path.suffix in {".yaml", ".yml"}:
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError("skill manifest not found")

    def _prepare_runtime_environment(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        manifest: Mapping[str, Any],
        skill_dir: Path,
    ) -> tuple[Path, list[str]]:
        runtime_cfg = manifest.get("runtime") or {}
        runtime_type = (runtime_cfg.get("type") or ("python" if "python" in runtime_cfg else "python")).lower()
        if runtime_type == "python":
            return self._prepare_python_runtime(
                env=env,
                slot=slot,
                manifest=manifest,
                runtime_cfg=runtime_cfg,
                skill_dir=skill_dir,
            )
        raise NotImplementedError(f"runtime type '{runtime_type}' is not supported")

    def _stage_skill_sources(self, source: Path, slot: SkillSlotPaths) -> Path:
        destination_root = slot.src_dir
        namespace_root = destination_root / "skills"
        target = namespace_root / source.name
        if destination_root.exists():
            self._remove_tree(destination_root)
        namespace_root.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo", ".runtime")
        shutil.copytree(source, target, ignore=ignore)
        package_init = target / "__init__.py"
        if not package_init.exists():
            package_init.write_text("", encoding="utf-8")
        handlers_dir = target / "handlers"
        handler_main = handlers_dir / "main.py"
        if not handler_main.exists():
            raise FileNotFoundError(f"handler entrypoint missing: {handler_main}")
        handlers_init = handlers_dir / "__init__.py"
        if not handlers_init.exists():
            handlers_init.write_text(
                "try:\n"
                "    from .main import handle  # noqa: F401\n"
                "except ImportError:\n"
                "    handle = None  # type: ignore[assignment]\n",
                encoding="utf-8",
            )
        return target

    def _smoke_import(self, *, env: SkillRuntimeEnvironment, name: str, version: str, slot: str | None = None) -> None:
        module_name = f"skills.{name}.handlers.main"
        slot_name = str(slot or "").strip().upper() or env.read_active_slot(version)
        try:
            slot_paths = env.build_slot_paths(version, slot_name)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise RuntimeError(f"failed to resolve slot {slot_name} for {name}: {exc}") from exc

        src_path = slot_paths.src_dir
        if not src_path.exists():
            raise RuntimeError(f"active slot for {name} lacks src directory: {src_path}")

        vendor_path = slot_paths.vendor_dir

        original_sys_path = list(sys.path)
        sdk_decorators = None
        registry_snapshot = None
        try:
            from adaos.sdk.core import decorators as sdk_decorators

            registry_snapshot = sdk_decorators._registry_snapshot()
        except Exception:
            _log.debug("failed to snapshot SDK decorator registries before smoke import", exc_info=True)
        try:
            runtime_vendor_fragment = f"/.runtime/{name}/"
            sys.path[:] = [
                entry
                for entry in sys.path
                if not (
                    runtime_vendor_fragment in entry.replace("\\", "/")
                    and (
                        entry.replace("\\", "/").endswith("/vendor")
                        or entry.replace("\\", "/").endswith("/slots/current/src")
                        or entry.replace("\\", "/").endswith("/slots/A/src")
                        or entry.replace("\\", "/").endswith("/slots/B/src")
                    )
                )
            ]
            paths_to_add = []
            if vendor_path.is_dir():
                paths_to_add.append(str(vendor_path))
            paths_to_add.append(str(src_path))
            for candidate in reversed(paths_to_add):
                if candidate not in sys.path:
                    sys.path.insert(0, candidate)
            for mod in list(sys.modules.keys()):
                if mod == f"skills.{name}" or mod == module_name or mod.startswith(f"skills.{name}."):
                    sys.modules.pop(mod, None)
            importlib.invalidate_caches()
            importlib.import_module(module_name)
        except Exception as exc:
            raise RuntimeError(f"failed to import handler module for {name}: {exc}") from exc
        finally:
            sys.path[:] = original_sys_path
            for mod in list(sys.modules.keys()):
                if mod == f"skills.{name}" or mod == module_name or mod.startswith(f"skills.{name}."):
                    sys.modules.pop(mod, None)
            if sdk_decorators is not None and registry_snapshot is not None:
                try:
                    sdk_decorators._restore_registry_snapshot(registry_snapshot)
                except Exception:
                    _log.warning("failed to restore SDK decorator registries after smoke import", exc_info=True)

    def _prepare_python_runtime(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        manifest: Mapping[str, Any],
        runtime_cfg: Mapping[str, Any],
        skill_dir: Path,
    ) -> tuple[Path, list[str]]:
        interpreter = Path(sys.executable)
        python_paths = self._install_python_dependencies(
            manifest=manifest,
            slot=slot,
            skill_dir=skill_dir,
        )
        self._sync_skill_env(env=env, skill_dir=skill_dir, slot=slot)
        return interpreter, python_paths

    def _install_python_dependencies(
        self,
        *,
        manifest: Mapping[str, Any],
        slot: SkillSlotPaths,
        skill_dir: Path,
    ) -> list[str]:
        runtime_cfg = manifest.get("runtime") or {}
        if isinstance(runtime_cfg, Mapping) and runtime_cfg.get("kind") == "service":
            # Service skills manage dependencies in their own environment
            # (see ServiceSkillSupervisor). Never install them into the hub venv.
            return []

        requirements_file = skill_dir / "requirements.in"
        dependencies = resolve_skill_dependency_args(
            self._collect_dependencies(manifest),
            skill_dir=skill_dir,
            repo_root=self._repo_root_for_dependency_resolution(),
        )
        python_args: list[str] = []
        if requirements_file.exists():
            python_args.extend(["-r", str(requirements_file)])
        if dependencies:
            python_args.extend(dependencies)

        if not python_args:
            return []

        constraints = self._constraints_file()
        base_cmd = [
            str(sys.executable),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
        ]
        if constraints:
            base_cmd.extend(["-c", str(constraints)])

        shared_cmd = [*base_cmd, *python_args]
        vendor_dir = slot.vendor_dir
        
        run_cwd = skill_dir if skill_dir.exists() else slot.src_dir

        def _run(cmd: list[str]) -> tuple[bool, str]:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(run_cwd))
            except FileNotFoundError as e:
                return False, str(e)
            ok = (p.returncode == 0)
            out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
            return ok, out

        # 1) Try pip in current interpreter; bootstrap pip if missing
        ok, out = _run(shared_cmd)
        if not ok and ("No module named pip" in out or "No module named pip" in out.replace("\r", "\n")):
            _run([str(sys.executable), "-m", "ensurepip", "--upgrade"])  # best-effort
            ok, out = _run(shared_cmd)
        if ok:
            # clean vendor if present
            if vendor_dir.exists():
                for child in vendor_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except FileNotFoundError:
                            pass
            return []

        # 2) Fallback: pip --target vendor (after ensurepip)
        vendor_dir.mkdir(parents=True, exist_ok=True)
        vendor_cmd = [
            *base_cmd,
            "--target",
            str(vendor_dir),
            "--no-warn-script-location",
            *python_args,
        ]
        ok2, out2 = _run(vendor_cmd)
        if not ok2 and ("No module named pip" in out2 or "No module named pip" in out2.replace("\r", "\n")):
            _run([str(sys.executable), "-m", "ensurepip", "--upgrade"])  # best-effort
            ok2, out2 = _run(vendor_cmd)
        if ok2:
            return [str(vendor_dir)]

        # 3) Last resort: try `uv pip install` (if available)
        uv_base = ["uv", "pip", "install", "--upgrade"]
        if constraints:
            uv_base.extend(["-c", str(constraints)])
        ok3, out3 = _run([*uv_base, *python_args])
        if ok3:
            # uv installs into environment; keep vendor clean
            if vendor_dir.exists():
                for child in vendor_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except FileNotFoundError:
                            pass
            return []

        # Try uv with --target vendor
        uv_vendor = [*uv_base, "--target", str(vendor_dir), *python_args]
        ok4, out4 = _run(uv_vendor)
        if ok4:
            return [str(vendor_dir)]

        # Failed all strategies
        raise RuntimeError(
            f"failed to install dependencies for skill '{slot.skill_name}':\n"
            f"pip(shared) -> {out}\n"
            f"pip(target) -> {out2}\n"
            f"uv(shared) -> {out3}\n"
            f"uv(target) -> {out4}"
        )

    def _constraints_file(self) -> Path | None:
        candidates: list[Path] = []
        workspace = self.ctx.paths.workspace_dir()
        candidates.append(workspace / "constraints.txt")
        candidates.append(workspace / "requirements" / "constraints.txt")
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        if package_dir:
            package_root = Path(package_dir).resolve().parent
            candidates.append(package_root / "constraints.txt")
            candidates.append(package_root / "requirements" / "constraints.txt")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _repo_root_for_dependency_resolution(self) -> Path | None:
        repo_root_attr = getattr(self.ctx.paths, "repo_root", None)
        try:
            repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
        except Exception:
            return None
        if not repo_root:
            return None
        try:
            return Path(repo_root).expanduser().resolve()
        except Exception:
            return None

    def _collect_dependencies(self, manifest: Mapping[str, Any]) -> list[str]:
        deps = manifest.get("dependencies") or []
        runtime_cfg = manifest.get("runtime") or {}
        runtime_deps = runtime_cfg.get("dependencies") or []
        combined: list[str] = []
        for value in list(deps) + list(runtime_deps):
            if not value:
                continue
            if isinstance(value, str):
                combined.append(value)
            elif isinstance(value, Mapping) and "name" in value:
                version = value.get("version")
                combined.append(f"{value['name']}{version or ''}")
        return combined

    def _read_json_object(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def _deep_merge_json(self, base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in overlay.items():
            existing = merged.get(key)
            if isinstance(existing, dict) and isinstance(value, Mapping):
                merged[key] = self._deep_merge_json(existing, value)
            else:
                merged[key] = value
        return merged

    def _write_json_object(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _sync_skill_env(self, *, env: SkillRuntimeEnvironment, skill_dir: Path, slot: SkillSlotPaths) -> None:
        store_path = slot.skill_env_path
        staged_skill_root = slot.src_dir / "skills" / slot.skill_name
        merged: dict[str, Any] = {}
        found = False
        candidates = [
            skill_dir / ".skill_env.json",
            skill_dir / ".skill_memory.json",
            staged_skill_root / ".skill_env.json",
            staged_skill_root / ".skill_memory.json",
            slot.legacy_skill_memory_path,
            slot.legacy_skill_env_path,
            slot.files_dir / ".skill_env.json",
            store_path,
        ]
        for candidate in candidates:
            if not candidate.exists() or not candidate.is_file():
                continue
            payload = self._read_json_object(candidate)
            if not payload:
                continue
            merged = self._deep_merge_json(merged, payload)
            found = True
        if found:
            self._write_json_object(store_path, merged)

    def _remove_tree_contents(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for child in list(path.iterdir()):
            if child.is_dir():
                self._remove_tree(child)
            else:
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass

    def _copy_tree_contents(self, source: Path, target: Path) -> int:
        self._remove_tree_contents(target)
        copied = 0
        if not source.exists():
            return copied
        for child in source.iterdir():
            destination = target / child.name
            if child.is_dir():
                shutil.copytree(child, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, destination)
            copied += 1
        return copied

    def _data_migration_config(self, resolved_manifest: Mapping[str, Any]) -> dict[str, Any]:
        config = resolved_manifest.get("data_migration")
        if isinstance(config, Mapping):
            return dict(config)
        tool_name = resolved_manifest.get("data_migration_tool")
        if isinstance(tool_name, str) and tool_name.strip():
            return {"tool": tool_name.strip()}
        return {}

    def _data_migration_file(self, *, skill_dir: Path, config: Mapping[str, Any]) -> Path | None:
        raw = config.get("file") if isinstance(config, Mapping) else None
        relative = str(raw or "migrations/data_migration.py").strip()
        if not relative:
            relative = "migrations/data_migration.py"
        candidate = (skill_dir / relative).resolve()
        try:
            candidate.relative_to(skill_dir.resolve())
        except ValueError:
            raise ValueError(f"data migration file must stay inside skill source: {relative}") from None
        return candidate if candidate.is_file() else None

    def _lifecycle_config(self, resolved_manifest: Mapping[str, Any]) -> dict[str, str]:
        config = resolved_manifest.get("lifecycle")
        out: dict[str, str] = {}
        if isinstance(config, Mapping):
            for key in ("persist_before_switch", "after_activate", "rehydrate", "before_deactivate", "dispose", "drain"):
                value = config.get(key)
                if isinstance(value, str) and value.strip():
                    out[key] = value.strip()
        for key in ("persist_before_switch", "after_activate", "rehydrate", "before_deactivate", "dispose", "drain"):
            value = resolved_manifest.get(key)
            if isinstance(value, str) and value.strip() and key not in out:
                out[key] = value.strip()
        return out

    def _prepared_lifecycle_state(self) -> dict[str, Any]:
        return {
            "persist": {"ok": False, "skipped": True, "reason": "not_activated"},
            "migrate": {},
            "drain": {"ok": False, "skipped": True, "reason": "not_triggered"},
            "dispose": {"ok": False, "skipped": True, "reason": "not_triggered"},
            "after_activate": {"ok": False, "skipped": True, "reason": "not_activated"},
            "rehydrate": {"ok": False, "skipped": True, "reason": "not_activated"},
            "before_deactivate": {"ok": False, "skipped": True, "reason": "not_triggered"},
            "rollback": {"ok": False, "skipped": True, "reason": "not_triggered"},
            "healthcheck": {},
        }

    def _slot_lifecycle_state(self, *, metadata: Mapping[str, Any], slot: str) -> dict[str, Any]:
        slots = metadata.get("slots")
        if isinstance(slots, Mapping):
            current = slots.get(slot)
            if isinstance(current, Mapping):
                value = current.get("lifecycle")
                if isinstance(value, Mapping):
                    state = self._prepared_lifecycle_state()
                    state.update({str(k): v for k, v in value.items()})
                    return state
        return self._prepared_lifecycle_state()

    def _read_json_dict(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _run_slot_tool(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        resolved_manifest: Mapping[str, Any],
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        tools = resolved_manifest.get("tools") or {}
        tool_spec = tools.get(tool_name) if isinstance(tools, Mapping) else None
        if not isinstance(tool_spec, Mapping):
            raise KeyError(f"lifecycle tool '{tool_name}' not found in resolved manifest")
        module = tool_spec.get("module")
        attr = tool_spec.get("callable") or tool_name
        runtime_cfg = resolved_manifest.get("runtime") if isinstance(resolved_manifest.get("runtime"), Mapping) else {}
        python_paths = runtime_cfg.get("python_paths") if isinstance(runtime_cfg, Mapping) else []
        extra_paths = [Path(p) for p in python_paths if isinstance(p, str) and p]

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH")
        prev_internal_root = os.environ.get("ADAOS_SKILL_INTERNAL_DATA_ROOT")
        prev_internal_active = os.environ.get("ADAOS_SKILL_INTERNAL_ACTIVE_PATH")
        prev_internal_target = os.environ.get("ADAOS_SKILL_INTERNAL_TARGET_PATH")
        prev_secrets = ctx.secrets
        ctx.secrets = SecretsService(SkillSecretsBackend(slot.data_root / "files" / "secrets.json"), ctx.caps)
        skill_dir = slot.src_dir / "skills" / slot.skill_name

        def _call_tool() -> Any:
            with use_ctx(ctx):
                return execute_tool(
                    skill_dir,
                    module=module,
                    attr=attr,
                    payload=payload,
                    extra_paths=extra_paths,
                )

        try:
            if not ctx.skill_ctx.set(slot.skill_name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{slot.skill_name}'")
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(slot.skill_env_path)
            os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(slot.skill_memory_path)
            os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = str(slot.internal_data_dir)
            os.environ["ADAOS_SKILL_INTERNAL_ACTIVE_PATH"] = str(slot.internal_data_dir)
            os.environ["ADAOS_SKILL_INTERNAL_TARGET_PATH"] = str(slot.internal_data_dir)
            result = _call_tool()
        finally:
            ctx.secrets = prev_secrets
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env
            if prev_memory is None:
                os.environ.pop("ADAOS_SKILL_MEMORY_PATH", None)
            else:
                os.environ["ADAOS_SKILL_MEMORY_PATH"] = prev_memory
            if prev_internal_root is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_DATA_ROOT", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = prev_internal_root
            if prev_internal_active is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_ACTIVE_PATH", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_ACTIVE_PATH"] = prev_internal_active
            if prev_internal_target is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_TARGET_PATH", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_TARGET_PATH"] = prev_internal_target

        return result if isinstance(result, dict) else {"result": result}

    def _run_data_migration_tool(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        resolved_manifest: Mapping[str, Any],
        skill_dir: Path,
        python_paths: Iterable[str],
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        tools = resolved_manifest.get("tools") or {}
        tool_spec = tools.get(tool_name) if isinstance(tools, Mapping) else None
        if not isinstance(tool_spec, Mapping):
            raise KeyError(f"data migration tool '{tool_name}' not found in resolved manifest")
        module = tool_spec.get("module")
        attr = tool_spec.get("callable") or tool_name
        extra_paths = [Path(p) for p in python_paths if p]

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH")
        prev_internal_root = os.environ.get("ADAOS_SKILL_INTERNAL_DATA_ROOT")
        prev_internal_active = os.environ.get("ADAOS_SKILL_INTERNAL_ACTIVE_PATH")
        prev_internal_target = os.environ.get("ADAOS_SKILL_INTERNAL_TARGET_PATH")
        prev_secrets = ctx.secrets
        ctx.secrets = SecretsService(SkillSecretsBackend(slot.data_root / "files" / "secrets.json"), ctx.caps)

        def _call_tool() -> Any:
            with use_ctx(ctx):
                return execute_tool(
                    skill_dir,
                    module=module,
                    attr=attr,
                    payload=payload,
                    extra_paths=extra_paths,
                )

        try:
            if not ctx.skill_ctx.set(slot.skill_name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{slot.skill_name}'")
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(slot.skill_env_path)
            os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(slot.skill_memory_path)
            os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = str(slot.internal_data_dir)
            os.environ["ADAOS_SKILL_INTERNAL_ACTIVE_PATH"] = str(payload.get("source_internal_dir") or "")
            os.environ["ADAOS_SKILL_INTERNAL_TARGET_PATH"] = str(payload.get("target_internal_dir") or "")
            result = _call_tool()
        finally:
            ctx.secrets = prev_secrets
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env
            if prev_memory is None:
                os.environ.pop("ADAOS_SKILL_MEMORY_PATH", None)
            else:
                os.environ["ADAOS_SKILL_MEMORY_PATH"] = prev_memory
            if prev_internal_root is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_DATA_ROOT", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = prev_internal_root
            if prev_internal_active is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_ACTIVE_PATH", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_ACTIVE_PATH"] = prev_internal_active
            if prev_internal_target is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_TARGET_PATH", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_TARGET_PATH"] = prev_internal_target

        return result if isinstance(result, dict) else {"result": result}

    def _run_data_migration_file(
        self,
        *,
        slot: SkillSlotPaths,
        migration_file: Path,
        skill_dir: Path,
        python_paths: Iterable[str],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        spec = importlib.util.spec_from_file_location(
            f"adaos_skill_{slot.skill_name}_data_migration",
            migration_file,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to load data migration file: {migration_file}")
        module = importlib.util.module_from_spec(spec)
        extra_paths = [str(Path(p)) for p in python_paths if p]
        src_root = str(slot.src_dir)

        ctx = self.ctx
        previous = ctx.skill_ctx.get()
        prev_env = os.environ.get("ADAOS_SKILL_ENV_PATH")
        prev_memory = os.environ.get("ADAOS_SKILL_MEMORY_PATH")
        prev_internal_root = os.environ.get("ADAOS_SKILL_INTERNAL_DATA_ROOT")
        prev_internal_active = os.environ.get("ADAOS_SKILL_INTERNAL_ACTIVE_PATH")
        prev_internal_target = os.environ.get("ADAOS_SKILL_INTERNAL_TARGET_PATH")
        prev_secrets = ctx.secrets
        original_sys_path = list(sys.path)
        ctx.secrets = SecretsService(SkillSecretsBackend(slot.data_root / "files" / "secrets.json"), ctx.caps)

        try:
            if not ctx.skill_ctx.set(slot.skill_name, skill_dir):
                raise RuntimeError(f"failed to establish context for skill '{slot.skill_name}'")
            os.environ["ADAOS_SKILL_ENV_PATH"] = str(slot.skill_env_path)
            os.environ["ADAOS_SKILL_MEMORY_PATH"] = str(slot.skill_memory_path)
            os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = str(slot.internal_data_dir)
            os.environ["ADAOS_SKILL_INTERNAL_ACTIVE_PATH"] = str(payload.get("source_internal_dir") or "")
            os.environ["ADAOS_SKILL_INTERNAL_TARGET_PATH"] = str(payload.get("target_internal_dir") or "")
            for candidate in reversed([src_root, *extra_paths]):
                if candidate and candidate not in sys.path:
                    sys.path.insert(0, candidate)
            with use_ctx(ctx):
                spec.loader.exec_module(module)
                migrate = getattr(module, "migrate", None)
                if not callable(migrate):
                    raise AttributeError(f"data migration file must expose migrate(payload): {migration_file}")
                result = migrate(dict(payload))
        finally:
            sys.path[:] = original_sys_path
            ctx.secrets = prev_secrets
            if previous is None:
                ctx.skill_ctx.clear()
            else:
                ctx.skill_ctx.set(previous.name, Path(previous.path))
            if prev_env is None:
                os.environ.pop("ADAOS_SKILL_ENV_PATH", None)
            else:
                os.environ["ADAOS_SKILL_ENV_PATH"] = prev_env
            if prev_memory is None:
                os.environ.pop("ADAOS_SKILL_MEMORY_PATH", None)
            else:
                os.environ["ADAOS_SKILL_MEMORY_PATH"] = prev_memory
            if prev_internal_root is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_DATA_ROOT", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_DATA_ROOT"] = prev_internal_root
            if prev_internal_active is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_ACTIVE_PATH", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_ACTIVE_PATH"] = prev_internal_active
            if prev_internal_target is None:
                os.environ.pop("ADAOS_SKILL_INTERNAL_TARGET_PATH", None)
            else:
                os.environ["ADAOS_SKILL_INTERNAL_TARGET_PATH"] = prev_internal_target

        return result if isinstance(result, dict) else {"result": result}

    def _invoke_slot_lifecycle_hook(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        resolved_manifest: Mapping[str, Any],
        hook_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        config = self._lifecycle_config(resolved_manifest)
        tool_name = str(config.get(hook_key) or "").strip()
        if not tool_name:
            return {"ok": True, "skipped": True, "hook": hook_key, "tool": ""}
        result = self._run_slot_tool(
            env=env,
            slot=slot,
            resolved_manifest=resolved_manifest,
            tool_name=tool_name,
            payload=payload,
        )
        return {"ok": True, "skipped": False, "hook": hook_key, "tool": tool_name, "result": result}

    def _invoke_persist_before_switch(
        self,
        *,
        env: SkillRuntimeEnvironment,
        name: str,
        target_version: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        current_version = env.resolve_active_version()
        if not current_version:
            return {"ok": True, "skipped": True, "hook": "persist_before_switch", "reason": "no_active_version"}
        current_slot = env.read_active_slot(current_version)
        current_paths = env.build_slot_paths(current_version, current_slot)
        manifest_path = current_paths.resolved_manifest
        resolved_manifest = self._read_json_dict(manifest_path)
        if not resolved_manifest:
            return {"ok": True, "skipped": True, "hook": "persist_before_switch", "reason": "no_resolved_manifest"}
        return self._invoke_slot_lifecycle_hook(
            env=env,
            slot=current_paths,
            resolved_manifest=resolved_manifest,
            hook_key="persist_before_switch",
            payload={
                "skill": name,
                "current_version": current_version,
                "current_slot": current_slot,
                "target_version": target_version,
                "state": "persist_before_switch",
            },
        )

    def _invoke_shutdown_hooks(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        resolved_manifest: Mapping[str, Any],
        payload: Mapping[str, Any],
        hooks: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        hook_chain = tuple(str(item or "").strip() for item in (hooks or ("drain", "dispose", "before_deactivate")) if str(item or "").strip())
        for hook_key in hook_chain:
            try:
                results[hook_key] = self._invoke_slot_lifecycle_hook(
                    env=env,
                    slot=slot,
                    resolved_manifest=resolved_manifest,
                    hook_key=hook_key,
                    payload=payload,
                )
            except Exception as exc:
                results[hook_key] = {
                    "ok": False,
                    "skipped": False,
                    "hook": hook_key,
                    "error": str(exc),
                }
        return results

    def shutdown_active_runtimes(
        self,
        *,
        reason: str = "runtime_shutdown",
        event_type: str = "subnet.stopping",
        hooks: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        hook_chain = tuple(str(item or "").strip() for item in (hooks or ("drain", "dispose", "before_deactivate")) if str(item or "").strip())
        items: list[dict[str, Any]] = []
        for row in self.reg.list():
            name = getattr(row, "name", None) or getattr(row, "id", None)
            if not name or not bool(getattr(row, "installed", True)):
                continue
            skill_name = str(name)
            entry: dict[str, Any] = {
                "skill": skill_name,
                "ok": True,
                "reason": reason,
                "event_type": event_type,
                "hooks": list(hook_chain),
                "active": False,
                "skipped": False,
            }
            try:
                status = self.runtime_status(skill_name)
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = str(exc)
                items.append(entry)
                continue
            entry["version"] = str(status.get("version") or "")
            entry["slot"] = str(status.get("active_slot") or "")
            entry["deactivated"] = bool(status.get("deactivated"))
            if bool(status.get("deactivated")):
                entry["skipped"] = True
                entry["reason_detail"] = str((status.get("deactivation") or {}).get("reason") or "already_deactivated")
                items.append(entry)
                continue
            version = str(status.get("version") or "").strip()
            slot_name = str(status.get("active_slot") or "").strip()
            if not version or not slot_name:
                entry["skipped"] = True
                entry["reason_detail"] = "inactive_runtime"
                items.append(entry)
                continue
            env = self._runtime_env(skill_name)
            slot = env.build_slot_paths(version, slot_name)
            resolved_manifest = self._read_json_dict(slot.resolved_manifest)
            if not resolved_manifest:
                entry["skipped"] = True
                entry["reason_detail"] = "missing_resolved_manifest"
                items.append(entry)
                continue
            metadata = env.read_version_metadata(version)
            lifecycle = self._slot_lifecycle_state(metadata=metadata, slot=slot_name)
            shutdown_payload = {
                "skill": skill_name,
                "version": version,
                "slot": slot_name,
                "reason": reason,
                "event_type": event_type,
                "state": "runtime_shutdown",
            }
            results = self._invoke_shutdown_hooks(
                env=env,
                slot=slot,
                resolved_manifest=resolved_manifest,
                payload=shutdown_payload,
                hooks=hook_chain,
            )
            lifecycle.update(results)
            metadata.setdefault("slots", {}).setdefault(slot_name, {})["lifecycle"] = lifecycle
            env.write_version_metadata(version, metadata)
            entry["active"] = True
            entry["lifecycle"] = lifecycle
            entry["hooks_result"] = results
            if any(isinstance(payload, Mapping) and payload.get("ok") is False for payload in results.values()):
                entry["ok"] = False
            items.append(entry)

        failed = [item for item in items if not bool(item.get("ok"))]
        active_total = sum(1 for item in items if bool(item.get("active")))
        skipped_total = sum(1 for item in items if bool(item.get("skipped")))
        return {
            "ok": not failed,
            "reason": reason,
            "event_type": event_type,
            "hooks": list(hook_chain),
            "total": len(items),
            "active_total": active_total,
            "failed_total": len(failed),
            "skipped_total": skipped_total,
            "skills": items,
        }

    def _restore_runtime_selection(
        self,
        *,
        env: SkillRuntimeEnvironment,
        previous_active_version: str | None,
        previous_active_slot: str | None,
        previous_deactivation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "restored_active_version": str(previous_active_version or ""),
            "restored_active_slot": str(previous_active_slot or ""),
            "restored_deactivation": bool(previous_deactivation),
        }
        try:
            if previous_active_version:
                env.prepare_version(previous_active_version)
                if previous_active_slot:
                    env.set_active_slot(previous_active_version, previous_active_slot)
                env.active_version_marker().write_text(previous_active_version, encoding="utf-8")
                if previous_active_slot:
                    env.record_active_selection(previous_active_version, previous_active_slot)
            else:
                try:
                    env.active_version_marker().unlink(missing_ok=True)
                except Exception:
                    pass
                env.clear_runtime_selection()
            if previous_deactivation:
                env.write_deactivation(dict(previous_deactivation))
            else:
                env.clear_deactivation()
        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
        return result

    def _prepare_bucket_data(
        self,
        *,
        env: SkillRuntimeEnvironment,
        slot: SkillSlotPaths,
        resolved_manifest: Mapping[str, Any],
        skill_dir: Path,
        python_paths: Iterable[str],
    ) -> dict[str, Any]:
        config = self._data_migration_config(resolved_manifest)
        target_version = str(resolved_manifest.get("version") or slot.version or "0.0.0")
        target_bucket = env.runtime_bucket(target_version)
        active_version = env.resolve_active_version()
        active_bucket = env.runtime_bucket(active_version) if active_version else ""
        target_data_root = slot.data_root
        target_dir = slot.internal_data_dir
        env.ensure_data_dirs(target_version)

        base_result = {
            "tool": "",
            "runtime_bucket": target_bucket,
            "data_root": str(target_data_root),
            "internal_root": str(target_dir),
            "target_version": target_version,
            "target_runtime_bucket": target_bucket,
            "bucket_migration": False,
        }
        if not active_version or active_bucket == target_bucket:
            return {
                **base_result,
                "mode": "shared",
                "skipped": True,
                "reason": "same_runtime_bucket" if active_version else "no_active_version",
                "source_version": str(active_version or ""),
                "source_runtime_bucket": str(active_bucket or ""),
            }

        metadata = env.read_version_metadata(target_version)
        previous_migration = metadata.get("bucket_data_migration")
        if isinstance(previous_migration, Mapping) and previous_migration.get("ok") is True:
            if previous_migration.get("source_runtime_bucket") == active_bucket:
                return {
                    **base_result,
                    "mode": "already_migrated",
                    "skipped": True,
                    "bucket_migration": True,
                    "source_version": active_version,
                    "source_runtime_bucket": active_bucket,
                    "previous": dict(previous_migration),
                }

        tool_name = str(config.get("tool") or "").strip()
        migration_file = self._data_migration_file(skill_dir=skill_dir, config=config)

        source_data_root = env.data_root(active_version)
        source_dir = source_data_root / "internal"
        if not tool_name and migration_file is None:
            copied = self._copy_tree_contents(source_data_root, target_data_root)
            env.ensure_data_dirs(target_version)
            _log.warning(
                "skill data migration file missing; copied bucket data without schema mutation skill=%s source_version=%s target_version=%s source_bucket=%s target_bucket=%s",
                slot.skill_name,
                active_version,
                target_version,
                active_bucket,
                target_bucket,
            )
            return {
                **base_result,
                "mode": "copy",
                "ok": True,
                "skipped": True,
                "reason": "data_migration_file_missing",
                "source_version": active_version,
                "source_runtime_bucket": active_bucket,
                "source_data_root": str(source_data_root),
                "target_data_root": str(target_data_root),
                "copied_entries": copied,
                "bucket_migration": True,
            }

        self._remove_tree_contents(target_data_root)
        env.ensure_data_dirs(target_version)
        payload = {
            "skill": slot.skill_name,
            "version": target_version,
            "runtime_slot": slot.slot,
            "source_version": active_version,
            "target_version": target_version,
            "source_runtime_bucket": active_bucket,
            "target_runtime_bucket": target_bucket,
            "source_data_root": str(source_data_root),
            "target_data_root": str(target_data_root),
            "source_internal_dir": str(source_dir),
            "target_internal_dir": str(target_dir),
            "data_root": str(target_data_root),
            "internal_root": str(target_dir),
            "source_exists": source_data_root.exists(),
        }
        try:
            if migration_file is not None:
                result = self._run_data_migration_file(
                    slot=slot,
                    migration_file=migration_file,
                    skill_dir=skill_dir,
                    python_paths=python_paths,
                    payload=payload,
                )
                mode = "file"
                tool_label = str(migration_file.relative_to(skill_dir))
            else:
                result = self._run_data_migration_tool(
                    env=env,
                    slot=slot,
                    resolved_manifest=resolved_manifest,
                    skill_dir=skill_dir,
                    python_paths=python_paths,
                    tool_name=tool_name,
                    payload=payload,
                )
                mode = "tool"
                tool_label = tool_name
        except Exception:
            self._remove_tree_contents(target_data_root)
            env.ensure_data_dirs(target_version)
            raise

        return {
            **base_result,
            "mode": mode,
            "ok": True,
            "skipped": False,
            "tool": tool_label,
            "source_version": active_version,
            "source_runtime_bucket": active_bucket,
            "source_internal_dir": str(source_dir),
            "target_internal_dir": str(target_dir),
            "source_data_root": str(source_data_root),
            "target_data_root": str(target_data_root),
            "bucket_migration": True,
            "result": result,
        }

    def _persist_skill_env(self, env: SkillRuntimeEnvironment, slot: SkillSlotPaths) -> None:
        source = slot.skill_env_path
        if not source.exists():
            return
        target = slot.skill_env_path
        if source.resolve() == target.resolve():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def _slot_version(
        self,
        *,
        env: SkillRuntimeEnvironment,
        metadata: Mapping[str, Any],
        slot: str,
        fallback: str,
    ) -> str:
        slot_meta = (metadata.get("slots") or {}).get(slot) if isinstance(metadata.get("slots"), Mapping) else None
        if isinstance(slot_meta, Mapping):
            value = slot_meta.get("version")
            if isinstance(value, str) and value.strip():
                return value.strip()
            resolved = slot_meta.get("resolved_manifest")
            if isinstance(resolved, str) and resolved.strip():
                payload = self._read_json_dict(Path(resolved))
                value = payload.get("version")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        candidate = env.build_slot_paths(fallback, slot).resolved_manifest
        payload = self._read_json_dict(candidate)
        value = payload.get("version")
        return value.strip() if isinstance(value, str) and value.strip() else fallback

    def _latest_prepared_version(self, env: SkillRuntimeEnvironment) -> Optional[str]:
        latest_version: Optional[str] = None
        latest_time: Optional[datetime] = None
        for version in env.list_versions():
            metadata = env.read_version_metadata(version)
            history = metadata.get("history", {})
            stamp = history.get("last_install_at")
            if not stamp:
                continue
            try:
                ts = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if latest_time is None or ts > latest_time:
                latest_time = ts
                latest_version = str(history.get("last_install_version") or version)
        return latest_version

    def _prune_runtime_history(
        self,
        *,
        env: SkillRuntimeEnvironment,
        current_version: str,
        previous_version: str | None,
    ) -> list[str]:
        keep_buckets = {env.runtime_bucket(current_version)}
        if previous_version:
            keep_buckets.add(env.runtime_bucket(previous_version))
        removed: list[str] = []
        if not env.runtime_root.exists():
            return removed
        for child in list(env.runtime_root.iterdir()):
            if not child.is_dir() or not env.is_runtime_bucket_name(child.name):
                continue
            if child.name in keep_buckets:
                continue
            try:
                self._remove_tree(child)
                removed.append(child.name)
            except Exception:
                _log.warning(
                    "failed to prune obsolete skill runtime bucket skill=%s bucket=%s current=%s previous=%s",
                    env.skill_name,
                    child.name,
                    current_version,
                    previous_version or "",
                    exc_info=True,
                )
        return removed

    def _preferred_activation_slot(
        self,
        env: SkillRuntimeEnvironment,
        version: str,
        metadata: Mapping[str, Any],
    ) -> str:
        history = metadata.get("history", {})
        preferred = history.get("last_install_slot")
        if preferred in {"A", "B"}:
            slot_paths = env.build_slot_paths(version, preferred)
            slot_meta = metadata.get("slots", {}).get(preferred, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if manifest_path.exists():
                return preferred
        return env.select_inactive_slot(version)

    def _policy_defaults(self) -> PolicyDefaults:
        settings = self.ctx.settings
        return PolicyDefaults(
            timeout_seconds=settings.default_wall_time_sec,
            retry_count=1,
            telemetry_enabled=True,
            sandbox_memory_mb=settings.default_max_rss_mb,
            sandbox_cpu_seconds=settings.default_cpu_time_sec,
        )

    def _policy_overrides(self) -> Dict[str, Any]:
        settings = self.ctx.settings
        return {
            "profile": getattr(settings, "profile", None),
            "default_wall_time_sec": getattr(settings, "default_wall_time_sec", None),
            "default_cpu_time_sec": getattr(settings, "default_cpu_time_sec", None),
            "default_max_rss_mb": getattr(settings, "default_max_rss_mb", None),
        }

    def _enrich_manifest(
        self,
        *,
        manifest: Mapping[str, Any],
        slot: SkillSlotPaths,
        interpreter: Path,
        python_paths: Iterable[str],
        defaults: PolicyDefaults,
        policy_overrides: Mapping[str, Any],
        skill_dir: Path,
    ) -> Dict[str, Any]:
        tools: Dict[str, Dict[str, Any]] = {}
        tool_entries = manifest.get("tools", []) or []
        default_tool = manifest.get("default_tool")
        for item in tool_entries:
            tool_name = item.get("name")
            if not tool_name:
                continue
            module_path, attr = self._resolve_tool_entry(tool_name, item, manifest)
            tools[tool_name] = {
                "name": tool_name,
                "module": module_path,
                "callable": attr,
                "timeout_seconds": item.get("timeout", defaults.timeout_seconds),
                "retries": item.get("retries", defaults.retry_count),
                "schema": {
                    "input": item.get("input_schema"),
                    "output": item.get("output_schema"),
                },
                "permissions": item.get("permissions") or manifest.get("permissions"),
                "secrets": self._preserve_secret_placeholders(item.get("secrets", [])),
            }
            for meta_key in ("side_effects", "read_only", "yjs_governance"):
                if meta_key in item:
                    tools[tool_name][meta_key] = item.get(meta_key)

        if not default_tool and len(tools) == 1:
            default_tool = next(iter(tools))

        data_migration = manifest.get("data_migration") if isinstance(manifest.get("data_migration"), Mapping) else {}
        data_migration_tool = str(
            data_migration.get("tool")
            or manifest.get("data_migration_tool")
            or ""
        ).strip()
        data_migration_entry = str(
            data_migration.get("entry")
            or data_migration.get("script")
            or manifest.get("data_migration_script")
            or ""
        ).strip()
        if not data_migration_tool and data_migration_entry:
            module_path, _, attr = data_migration_entry.partition(":")
            if module_path and attr:
                data_migration_tool = "__data_migration__"
                tools[data_migration_tool] = {
                    "name": data_migration_tool,
                    "module": module_path,
                    "callable": attr,
                    "timeout_seconds": defaults.timeout_seconds,
                    "retries": defaults.retry_count,
                    "schema": {"input": None, "output": None},
                    "permissions": manifest.get("permissions"),
                    "secrets": [],
                }
                data_migration = {**dict(data_migration), "tool": data_migration_tool, "entry": data_migration_entry}
        reserved_migration = skill_dir / "migrations" / "data_migration.py"
        if reserved_migration.is_file():
            data_migration = {
                **dict(data_migration),
                "file": "migrations/data_migration.py",
                "callable": "migrate",
            }

        return {
            "name": manifest.get("name", slot.skill_name),
            "version": manifest.get("version"),
            "runtime_bucket": slot.root.parent.parent.name,
            "slot": slot.slot,
            "source": str(skill_dir.resolve()),
            "runtime": {
                "type": (manifest.get("runtime") or {}).get("type", "python"),
                "bucket": slot.root.parent.parent.name,
                "interpreter": str(interpreter),
                "src": str(slot.src_dir),
                "vendor": str(slot.vendor_dir),
                "venv": str(slot.venv_dir),
                "runtime_dir": str(slot.runtime_dir),
                "logs": str(slot.logs_dir),
                "tmp": str(slot.tmp_dir),
                "tests": str(slot.tests_dir),
                "python_paths": list(python_paths),
                "skill_env": str(slot.skill_env_path),
                "skill_memory": str(slot.skill_memory_path),
                "internal_data": str(slot.internal_data_dir),
            },
            "tools": tools,
            "default_tool": default_tool,
            "data_migration_tool": data_migration_tool,
            "data_migration": dict(data_migration) if data_migration_tool or data_migration else {},
            "policy": {
                "timeout_seconds": defaults.timeout_seconds,
                "retry_count": defaults.retry_count,
                "telemetry_enabled": defaults.telemetry_enabled,
                "sandbox_memory_mb": defaults.sandbox_memory_mb,
                "sandbox_cpu_seconds": defaults.sandbox_cpu_seconds,
            },
            "policy_overrides": dict(policy_overrides),
            "secrets": self._preserve_secret_placeholders(manifest.get("secrets", [])),
            "events": manifest.get("events"),
            "slot_root": str(slot.root),
        }

    def _resolve_tool_entry(
        self,
        tool_name: str,
        item: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> tuple[str, str]:
        runtime_cfg = manifest.get("runtime") or {}
        entry = item.get("entry")
        if entry:
            module_path, _, attr = entry.partition(":")
            module_path = module_path or runtime_cfg.get("module") or "handlers.main"
            attr = attr or tool_name
            return module_path, attr
        module_path = runtime_cfg.get("module") or "handlers.main"
        return module_path, tool_name

    def _preserve_secret_placeholders(self, values: Iterable[Any]) -> list[Any]:
        preserved: list[Any] = []
        for value in values or []:
            if isinstance(value, str) and not value.startswith("${secret:"):
                preserved.append(f"${{secret:{value}}}")
            else:
                preserved.append(value)
        return preserved

    def _write_resolved_manifest(self, slot: SkillSlotPaths, payload: Mapping[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = slot.resolved_manifest.with_suffix(".tmp")
        slot.resolved_manifest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, slot.resolved_manifest)

    def _remove_tree(self, path: Path) -> None:
        if not path.exists():
            return
        for child in path.iterdir():
            if child.is_dir():
                self._remove_tree(child)
            else:
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass
        try:
            path.rmdir()
        except OSError:
            pass

    def prepare_dev_runtime(
        self,
        name: str,
        *,
        version_override: str | None = None,
        run_tests: bool = False,
        preferred_slot: str | None = None,
    ) -> RuntimeInstallResult:
        """Prepare a runtime for a DEV skill under .adaos/dev/<subnet>/skills.

        Mirrors prepare_runtime but uses the DEV skills root as the source and runtime root.
        """
        dev_root = self.ctx.paths.dev_skills_dir()
        skill_dir = dev_root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        try:
            manifest = self._load_manifest(skill_dir)
        except FileNotFoundError:
            manifest = {}
        version = version_override or str(manifest.get("version") or "dev")

        env = SkillRuntimeEnvironment(skills_root=dev_root, skill_name=name)
        env.prepare_version(version)

        slot_name = preferred_slot or env.select_inactive_slot(version)
        slot = env.build_slot_paths(version, slot_name)

        # Ensure clean slot state before preparing runtime
        env.cleanup_slot(version, slot_name)
        env.prepare_version(version)
        slot = env.build_slot_paths(version, slot_name)

        try:
            staged_dir = self._stage_skill_sources(skill_dir, slot)
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise

        try:
            interpreter, python_paths = self._prepare_runtime_environment(
                env=env,
                slot=slot,
                manifest=manifest,
                skill_dir=staged_dir,
            )
        except Exception:
            env.cleanup_slot(version, slot_name)
            raise
        defaults = self._policy_defaults()
        policy_overrides = self._policy_overrides()

        resolved = self._enrich_manifest(
            manifest=manifest,
            slot=slot,
            interpreter=interpreter,
            python_paths=python_paths,
            defaults=defaults,
            policy_overrides=policy_overrides,
            skill_dir=staged_dir,
        )
        self._write_resolved_manifest(slot, resolved)
        lifecycle = self._prepared_lifecycle_state()
        data_migration = self._prepare_bucket_data(
            env=env,
            slot=slot,
            resolved_manifest=resolved,
            skill_dir=staged_dir,
            python_paths=python_paths,
        )
        lifecycle["migrate"] = {"ok": True, "mode": str(data_migration.get("mode") or "shared")}

        tests: Dict[str, TestResult] = {}
        if run_tests:
            log_file = slot.logs_dir / "tests.log"
            tests = run_tests(
                staged_dir,
                log_path=log_file,
                interpreter=interpreter,
                python_paths=python_paths,
                skill_env_path=slot.skill_env_path,
                skill_name=name,
                skill_version=version,
                slot_current_dir=slot.root,
            )
            if any(result.status != "passed" for result in tests.values()):
                env.cleanup_slot(version, slot_name)
                raise RuntimeError("skill tests failed")
        lifecycle["healthcheck"] = {
            "ok": True,
            "stage": "prepare",
            "tests": {name: result.status for name, result in tests.items()},
        }

        metadata = env.read_version_metadata(version)
        slots_meta = metadata.setdefault("slots", {})
        slots_meta[slot_name] = {
            "version": version,
            "runtime_bucket": env.runtime_bucket(version),
            "resolved_manifest": str(slot.resolved_manifest),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "tests": {name: result.status for name, result in tests.items()},
            "data_migration": dict(data_migration),
            "lifecycle": dict(lifecycle),
        }
        metadata["version"] = version
        metadata["runtime_bucket"] = env.runtime_bucket(version)
        if data_migration.get("bucket_migration"):
            metadata["bucket_data_migration"] = dict(data_migration)
        history = metadata.setdefault("history", {})
        history["last_install_slot"] = slot_name
        history["last_install_version"] = version
        history["last_install_at"] = datetime.now(timezone.utc).isoformat()
        history["last_default_tool"] = resolved.get("default_tool")
        env.write_version_metadata(version, metadata)

        return RuntimeInstallResult(
            name=name,
            version=version,
            slot=slot_name,
            resolved_manifest=slot.resolved_manifest,
            tests=tests,
            data_migration=dict(data_migration),
            lifecycle=dict(lifecycle),
        )

    def activate_dev_runtime(self, name: str, *, version: str | None = None, slot: str | None = None) -> str:
        """Activate a prepared DEV runtime (under .adaos/dev/<subnet>/skills).

        If the requested version/slot is not prepared yet, prepare from the DEV skill sources first.
        """
        env = self._runtime_env_dev(name)
        dev_root = self.ctx.paths.dev_skills_dir()
        skill_dir = dev_root / name
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill '{name}' not found at {skill_dir}")

        target_version = version or env.resolve_active_version()
        if not target_version:
            # derive version from manifest, default to 'dev'
            try:
                manifest = self._load_manifest(skill_dir)
            except FileNotFoundError:
                manifest = {}
            target_version = str(manifest.get("version") or "dev")

        # Ensure version layout exists and slot is prepared
        env.prepare_version(target_version)
        metadata = env.read_version_metadata(target_version)
        target_slot = slot or self._preferred_activation_slot(env, target_version, metadata)
        slot_paths = env.build_slot_paths(target_version, target_slot)
        slot_meta = metadata.get("slots", {}).get(target_slot, {})
        manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
        if not manifest_path.exists():
            # prepare from DEV sources when missing
            self.prepare_dev_runtime(name, version_override=target_version, run_tests=False, preferred_slot=target_slot)
            metadata = env.read_version_metadata(target_version)
            slot_meta = metadata.get("slots", {}).get(target_slot, {})
            manifest_path = Path(slot_meta.get("resolved_manifest") or slot_paths.resolved_manifest)
            if not manifest_path.exists():
                raise RuntimeError(f"slot {target_slot} of version {target_version} is not prepared")

        target_manifest = self._read_json_dict(manifest_path)
        lifecycle = self._slot_lifecycle_state(metadata=metadata, slot=target_slot)
        lifecycle["persist"] = {"ok": True, "skipped": True, "hook": "persist_before_switch", "reason": "dev_runtime"}
        previous_active_version = env.resolve_active_version()
        previous_active_slot = env.read_active_slot(previous_active_version) if previous_active_version else None
        previous_deactivation = env.read_deactivation()
        env.set_active_slot(target_version, target_slot)
        env.active_version_marker().write_text(target_version, encoding="utf-8")
        env.record_active_selection(
            target_version,
            target_slot,
            previous_version=previous_active_version,
            previous_slot=previous_active_slot,
        )
        try:
            lifecycle["after_activate"] = self._invoke_slot_lifecycle_hook(
                env=env,
                slot=slot_paths,
                resolved_manifest=target_manifest,
                hook_key="after_activate",
                payload={"skill": name, "version": target_version, "slot": target_slot, "state": "active", "dev": True},
            )
            lifecycle["rehydrate"] = self._invoke_slot_lifecycle_hook(
                env=env,
                slot=slot_paths,
                resolved_manifest=target_manifest,
                hook_key="rehydrate",
                payload={"skill": name, "version": target_version, "slot": target_slot, "state": "active", "dev": True},
            )
            lifecycle["healthcheck"] = {"ok": True, "stage": "activate"}
        except Exception as exc:
            lifecycle["healthcheck"] = {"ok": False, "stage": "activate", "error": str(exc)}
            lifecycle.update(
                self._invoke_shutdown_hooks(
                    env=env,
                    slot=slot_paths,
                    resolved_manifest=target_manifest,
                    payload={
                        "skill": name,
                        "version": target_version,
                        "slot": target_slot,
                        "reason": "activation_rehydrate_failed",
                        "state": "deactivating",
                        "dev": True,
                    },
                )
            )
            lifecycle["rollback"] = self._restore_runtime_selection(
                env=env,
                previous_active_version=previous_active_version,
                previous_active_slot=previous_active_slot,
                previous_deactivation=previous_deactivation,
            )
            metadata.setdefault("slots", {}).setdefault(target_slot, {})["lifecycle"] = dict(lifecycle)
            env.write_version_metadata(target_version, metadata)
            raise RuntimeError(f"activation rehydrate failed: {exc}") from exc
        history = metadata.setdefault("history", {})
        history["last_active_slot"] = target_slot
        history["last_active_version"] = target_version
        history["last_active_at"] = datetime.now(timezone.utc).isoformat()
        target_slot_meta = metadata.setdefault("slots", {}).setdefault(target_slot, {})
        target_slot_meta.setdefault("version", target_version)
        target_slot_meta.setdefault("runtime_bucket", env.runtime_bucket(target_version))
        target_slot_meta["lifecycle"] = dict(lifecycle)
        env.write_version_metadata(target_version, metadata)
        self._prune_runtime_history(env=env, current_version=target_version, previous_version=previous_active_version)
        self._smoke_import(env=env, name=name, version=target_version)
        return target_slot

    def run_dev_skill_tests(self, name: str) -> Dict[str, TestResult]:
        """Запуск тестов DEV-навыка прямо из исходников (без install/slots/.runtime).
        - Ищем тесты в <dev>/skills/<name>/tests/**/*.py (pytest discovery).
        - Логи пишем в bucket data текущей DEV-версии.
        - Запрещаем произвольные пути; только внутри DEV root.
        """
        self.caps.require("core", "skills.manage")

        sub = name.strip()
        if not _name_re.match(sub):
            raise ValueError("invalid skill name")

        dev_root = self.ctx.paths.dev_skills_dir()
        skill_dir = (dev_root / sub).resolve()
        try:
            skill_dir.relative_to(dev_root)
        except ValueError:
            raise PermissionError("skill path escapes dev root")
        if not skill_dir.exists() or not skill_dir.is_dir():
            raise FileNotFoundError(f"skill '{name}' not found in DEV at {skill_dir}")
        # Манифест нужен только для подсказок рантайма; отсутствие не фатально
        try:
            manifest = self._load_manifest(skill_dir)
        except FileNotFoundError:
            manifest = {}

        runtime_info = manifest.get("runtime", {}) or {}
        interpreter_value = runtime_info.get("interpreter")
        interpreter = Path(interpreter_value) if interpreter_value else Path(sys.executable)

        # PYTHONPATH: из манифеста + корень пакета AdaOS (безопасно)
        python_paths: list[str] = [p for p in runtime_info.get("python_paths", []) if p]
        package_dir = getattr(self.ctx.paths, "package_dir", None)
        if callable(package_dir):
            package_dir = package_dir()
        package_root = Path(package_dir).resolve().parent if package_dir else None
        if package_root:
            python_paths.append(str(package_root))
        try:
            python_paths.append(str(self.ctx.paths.package_path()))
        except Exception:
            pass

        dev_dir = self.ctx.paths.dev_dir()  # ...\.adaos\dev\sn_xxxx\
        python_paths.insert(0, str(skill_dir))  # ...\.adaos\dev\sn_xxxx\skills\<name>\
        python_paths.insert(0, str(dev_dir))  # родитель 'skills' — нужен для 'import skills.*'

        extra_env = {
            "ADAOS_DEV_DIR": str(dev_dir),
            "ADAOS_DEV_SKILL_DIR": str(skill_dir),
            "ADAOS_SKILL_NAME": name,
            "ADAOS_SKILL_PACKAGE": f"skills.{name}",
        }

        env = self._runtime_env_dev(name)
        env.ensure_base()
        logs_dir = env.data_root() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "tests.dev.log"

        # Canonical runtime store lives under .runtime/<skill>/v<major>.<minor>/data/files/.skill_env.json.
        # A local .skill_env.json in the skill sources is treated as a template/seed only.
        skill_env_path: Path | None = None
        skill_env_raw = runtime_info.get("skill_env")
        if skill_env_raw:
            skill_env_path = Path(skill_env_raw)
        else:
            local_env = skill_dir / ".skill_env.json"
            if local_env.exists():
                skill_env_path = local_env

        # Запускаем тесты: источник — каталог навыка; pytest сам найдёт tests/**/*.py
        return run_tests(
            skill_dir,  # skill_source
            log_path=log_path,
            interpreter=interpreter,  # sys.executable или из манифеста
            python_paths=python_paths,  # из манифеста + package_root
            skill_env_path=skill_env_path,  # опционально
            skill_name=name,
            skill_version=manifest.get("version") or "dev",
            slot_current_dir=skill_dir,  # для совместимости сигнатуры; слотов нет
            dev_mode=True,
            extra_env=extra_env,
        )
