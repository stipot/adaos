# src\adaos\services\scenario\manager.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re, os, json
from pathlib import Path
from typing import Any, Optional, Literal

import yaml
from adaos.adapters.git.workspace import wait_for_materialized

from adaos.domain import SkillMeta, SkillRecord
from adaos.ports import EventBus, GitClient, Capabilities
from adaos.ports.paths import PathProvider
from adaos.ports.scenarios import ScenarioRepository
from adaos.services.eventbus import emit
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.fs.safe_io import remove_tree
from adaos.services.git.safe_commit import sanitize_message, check_no_denied
from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.services.capacity import install_scenario_in_capacity, uninstall_scenario_from_capacity
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.capacity import get_local_capacity
from adaos.domain.workspace_manifest import parse_scenario_skill_bindings
from adaos.services.node_config import load_config
from adaos.services.scenarios.loader import read_manifest, read_content
import y_py as Y
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc
from adaos.services.yjs.store import ystore_write_metadata, ystore_write_metadata_sync
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.skill.manager import SkillManager
from adaos.services.semver import bump_version
from adaos.services.workspace_registry import upsert_workspace_registry_entry

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")
_log = logging.getLogger("adaos.scenario.manager")
_RUNTIME_OWNED_DATA_KEYS = {"catalog", "installed", "desktop", "routing"}
_SCENARIO_MANIFEST_NAMES = ("scenario.yaml", "scenario.yml", "scenario.json")


def _env_type() -> str:
    return str(os.getenv("ENV_TYPE", "prod") or "prod").strip().lower()


def _local_node_id() -> str:
    try:
        conf = load_config()
        node_id = str(getattr(conf, "node_id", "") or "").strip()
        if node_id:
            return node_id
        nested = str(getattr(getattr(conf, "node_settings", None), "id", "") or "").strip()
        if nested:
            return nested
    except Exception:
        pass
    return "hub"


def _store_node_scoped_scenario_entry(
    scenarios_root: object,
    *,
    node_id: str,
    scenario_id: str,
    value: object,
) -> dict:
    root = dict(scenarios_root) if isinstance(scenarios_root, dict) else {}
    node_bucket = root.get(node_id)
    if not isinstance(node_bucket, dict):
        node_bucket = {}
    next_node_bucket = dict(node_bucket)
    next_node_bucket[scenario_id] = value
    root[node_id] = next_node_bucket
    return root


def _scenario_manager_async_write_meta():
    return ystore_write_metadata(
        root_names=["ui", "registry", "data"],
        source="scenario.manager",
        owner="core:scenario_manager",
        channel="core.scenario_manager.async",
    )


def _scenario_manager_sync_write_meta():
    return ystore_write_metadata_sync(
        root_names=["ui", "registry", "data"],
        source="scenario.manager",
        owner="core:scenario_manager",
        channel="core.scenario_manager.sync",
    )


@dataclass(slots=True)
class ArtifactPublishResult:
    kind: str
    name: str
    source_path: Path
    target_path: Path
    version: str
    previous_version: str | None
    updated_at: str
    dry_run: bool = False
    warnings: tuple[str, ...] = ()


class ScenarioManager:
    """Coordinate scenario lifecycle operations via repository and registry."""

    def __init__(
        self,
        *,
        repo: ScenarioRepository,
        registry: SqliteScenarioRegistry,
        git: GitClient,
        paths: PathProvider,
        bus: EventBus,
        caps: Capabilities,  # SqliteScenarioRegistry протоколом не ограничиваем
    ):
        self.repo, self.reg, self.git, self.paths, self.bus, self.caps = repo, registry, git, paths, bus, caps
        self.ctx: AgentContext = get_ctx()
        self.last_dependency_bootstrap_result: dict[str, Any] | None = None

    def list_installed(self) -> list[SkillRecord]:
        self.caps.require("core", "scenarios.manage")
        return self.reg.list()

    def list_present(self) -> list[SkillMeta]:
        self.caps.require("core", "scenarios.manage")
        self.repo.ensure()
        return self.ctx.scenarios_repo.list()  # .repo.list()

    def sync(self, *, force: bool | None = None) -> None:
        self.caps.require("core", "scenarios.manage", "net.git")
        self.repo.ensure()
        root = self.ctx.paths.workspace_dir()
        names = [r.name for r in self.reg.list()]
        skill_names: list[str] = []
        try:
            from adaos.adapters.db import SqliteSkillRegistry

            skill_names = [r.name for r in SqliteSkillRegistry(self.ctx.sql).list()]
        except Exception:
            skill_names = []

        # Workspace repo hosts both /skills and /scenarios under a single sparse-checkout.
        # Keep both sets in the sparse pattern list to avoid "disappearing" directories
        # when syncing one kind.
        prefixed = [
            ".gitignore",
            "registry.json",
            "schemas",
            *[f"skills/{n}" for n in skill_names],
            *[f"scenarios/{n}" for n in names],
        ]
        from adaos.services.git.workspace_guard import ensure_clean

        effective_force = (_env_type() != "dev") if force is None else bool(force)
        if effective_force:
            stash_ref = self.git.stash_push(
                str(root),
                "adaos:auto-stash forced scenario sync",
                include_untracked=True,
            )
            _log.warning(
                "forced scenario sync requested env_type=%s repo=%s stash=%s",
                _env_type(),
                str(root),
                str(stash_ref or "-"),
            )
        else:
            ensure_clean(self.git, str(root), prefixed)
        self.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.git.sparse_set(str(root), prefixed, no_cone=True)
        self.git.pull(str(root))
        emit(self.bus, "scenario.sync", {"count": len(names)}, "scenario.mgr")

    def install(self, name: str, *, pin: Optional[str] = None) -> SkillMeta:
        self.caps.require("core", "scenarios.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid scenario name")

        self.reg.register(name, pin=pin)
        try:
            # TODO move test_mode to global ctx (single truth)
            test_mode = os.getenv("ADAOS_TESTING") == "1"
            if test_mode:
                return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
            # 3) mono-only установка через репозиторий (sparse-add + pull)
            meta = self.ctx.scenarios_repo.install(name, branch=None)
            if not meta:
                raise FileNotFoundError(f"scenario '{name}' not found in monorepo")
            emit(self.bus, "scenario.installed", {"id": meta.id.value, "pin": pin}, "scenario.mgr")
            try:
                install_scenario_in_capacity(meta.id.value, getattr(meta, "version", "unknown"), active=True)
                try:
                    conf = load_config()
                    if conf.role == "hub":
                        cap = get_local_capacity()
                        get_directory().repo.replace_scenario_capacity(conf.node_id, (cap.get("scenarios") or []))
                except Exception:
                    pass
            except Exception:
                pass
            return meta
        except Exception:
            self.reg.unregister(name)
            raise

    # --- Stage A2 helpers -------------------------------------------------

    def install_with_deps(self, name: str, *, pin: Optional[str] = None, webspace_id: str | None = None) -> SkillMeta:
        """
        Install a scenario (as with :meth:`install`) and then apply its
        manifest-defined dependencies inside the local workspace:

          - read scenario.yaml and ensure dependent skills are installed
            and activated via :class:`SkillManager`;
          - keep Yjs/bootstrap concerns in y_bootstrap (Stage A2).
        """
        target_webspace = webspace_id or default_webspace_id()
        meta = self.install(name, pin=pin)
        try:
            dep_result = self.bootstrap_dependencies(meta.id.value, webspace_id=target_webspace)
        except Exception as exc:
            dep_result = {
                "ok": False,
                "scenario_id": getattr(getattr(meta, "id", None), "value", name),
                "webspace_id": target_webspace,
                "required": [],
                "items": [],
                "succeeded": [],
                "failed": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.last_dependency_bootstrap_result = dep_result
        try:
            self.sync_to_yjs(meta.id.value, webspace_id=target_webspace)
        except Exception:
            pass
        return meta

    def bootstrap_dependencies(self, scenario_id: str, *, webspace_id: str | None = None) -> dict[str, Any]:
        """
        Best-effort dependency activation for an already installed scenario.

        Split out from :meth:`install_with_deps` so async install operations can
        report progress phase-by-phase instead of blocking in one opaque step.
        """
        result = self._post_install_bootstrap(scenario_id, webspace_id=webspace_id)
        self.last_dependency_bootstrap_result = result
        return result

    def _ensure_yjs_payload(self, scenario_id: str, *, space: str = "workspace") -> tuple[dict, dict, dict, dict]:
        """
        Load and normalise declarative payload from scenario.json so that it can
        be projected into a webspace YDoc.

        The returned tuple is:
          - ui_section:    content["ui"]["application"]  (or {}),
          - registry:      content["registry"]           (or {}),
          - catalog:       content["catalog"]            (or {}),
          - data_section:  content["data"]               (or {}).

        This shape matches the target-state description in
        docs/concepts/scenarios-target-state.md and is the only input for the
        ScenarioManager.sync_to_yjs* projection helpers.
        """
        content = read_content(scenario_id, space=space)
        if not content:
            raise FileNotFoundError(f"scenario '{scenario_id}' has no scenario.json content")
        ui_section = ((content.get("ui") or {}).get("application")) or {}
        if not isinstance(ui_section, dict):
            ui_section = {}
        registry_section = content.get("registry") or {}
        if not isinstance(registry_section, dict):
            registry_section = {}
        catalog_section = content.get("catalog") or {}
        if not isinstance(catalog_section, dict):
            catalog_section = {}
        data_section = content.get("data") or {}
        if not isinstance(data_section, dict):
            data_section = {}

        # Debug trace to help diagnose scenario→YDoc projection issues without
        # failing the projection if logging is misconfigured.
        try:
            desktop = (ui_section.get("desktop") or {}) if isinstance(ui_section, dict) else {}
            topbar = desktop.get("topbar")
            _log.debug("scenario '%s' ui.desktop.topbar=%s", scenario_id, topbar)
        except Exception:
            pass

        return ui_section, registry_section, catalog_section, data_section

    def _project_to_doc(
        self,
        ydoc: Y.YDoc,
        scenario_id: str,
        ui_section: dict,
        registry_section: dict,
        catalog_section: dict,
        data_section: dict,
    ) -> None:
        node_id = _local_node_id()
        with ydoc.begin_transaction() as txn:
            ui_map = ydoc.get_map("ui")
            registry_map = ydoc.get_map("registry")
            data_map = ydoc.get_map("data")

            scenarios_ui = ui_map.get("scenarios")
            if not isinstance(scenarios_ui, dict):
                scenarios_ui = {}
            updated_ui = _store_node_scoped_scenario_entry(
                dict(scenarios_ui),
                node_id=node_id,
                scenario_id=scenario_id,
                value={"application": ui_section},
            )
            ui_map.set(txn, "scenarios", updated_ui)
            if not ui_map.get("current_scenario"):
                ui_map.set(txn, "current_scenario", scenario_id)

            reg_scenarios = registry_map.get("scenarios")
            if not isinstance(reg_scenarios, dict):
                reg_scenarios = {}
            reg_updated = _store_node_scoped_scenario_entry(
                dict(reg_scenarios),
                node_id=node_id,
                scenario_id=scenario_id,
                value=registry_section,
            )
            registry_map.set(txn, "scenarios", reg_updated)

            data_scenarios = data_map.get("scenarios")
            if not isinstance(data_scenarios, dict):
                data_scenarios = {}
            data_updated = _store_node_scoped_scenario_entry(
                dict(data_scenarios),
                node_id=node_id,
                scenario_id=scenario_id,
                value={"catalog": catalog_section},
            )
            data_map.set(txn, "scenarios", data_updated)

            # Optional root data overrides from scenario.json:
            # if "data" section is present in the scenario payload, its
            # top-level keys are projected into the webspace data map.
            # This is used for initial data seeding (e.g. data.weather)
            # and for YJS reload; only explicitly defined keys are
            # overwritten. Runtime-owned effective branches stay under
            # semantic rebuild ownership and are skipped here.
            if isinstance(data_section, dict):
                skipped_runtime_owned: list[str] = []
                for key, value in data_section.items():
                    if not isinstance(key, str):
                        continue
                    if key in _RUNTIME_OWNED_DATA_KEYS:
                        skipped_runtime_owned.append(key)
                        continue
                    try:
                        payload = json.loads(json.dumps(value))
                    except Exception:
                        payload = value
                    data_map.set(txn, key, payload)
                if skipped_runtime_owned:
                    _log.debug(
                        "scenario '%s' skipped runtime-owned data keys during Yjs projection: %s",
                        scenario_id,
                        ",".join(sorted(set(skipped_runtime_owned))),
                    )

    def project_scenario_to_doc(
        self,
        ydoc: Y.YDoc,
        scenario_id: str,
        *,
        space: str = "workspace",
    ) -> None:
        """
        Project one scenario directly into an already-open YDoc.

        This is the low-level single-pass helper for cold-open/bootstrap paths
        that already own a live in-memory document and want to avoid a second
        store-backed YDoc session.
        """
        self.caps.require("core", "scenarios.manage")
        ui_section, registry_section, catalog_section, data_section = self._ensure_yjs_payload(
            scenario_id,
            space=space,
        )
        self._project_to_doc(
            ydoc,
            scenario_id,
            ui_section,
            registry_section,
            catalog_section,
            data_section,
        )

    def sync_to_yjs(
        self,
        scenario_id: str,
        webspace_id: str | None = None,
        *,
        space: str = "workspace",
        emit_event: bool = True,
    ) -> None:
        """
        Project the declarative scenario payload into the webspace YDoc.

        This is the generic projection entry point described in the
        scenario target-state docs: it takes scenario.json, writes
        ui/data/registry sections into the YDoc, and emits a single
        ``scenarios.synced`` event that Webspace Scenario Runtime and other
        downstream services can react to.
        """
        target_webspace = webspace_id or default_webspace_id()
        self.caps.require("core", "scenarios.manage")
        ui_section, registry_section, catalog_section, data_section = self._ensure_yjs_payload(scenario_id, space=space)

        with _scenario_manager_sync_write_meta():
            with get_ydoc(target_webspace) as ydoc:
                self._project_to_doc(ydoc, scenario_id, ui_section, registry_section, catalog_section, data_section)

        if emit_event:
            emit(self.bus, "scenarios.synced", {"scenario_id": scenario_id, "webspace_id": target_webspace}, "scenario.mgr")

    async def sync_to_yjs_async(
        self,
        scenario_id: str,
        webspace_id: str | None = None,
        *,
        space: str = "workspace",
        emit_event: bool = True,
    ) -> None:
        """
        Async variant of :meth:`sync_to_yjs` for use inside running event loops.
        """
        target_webspace = webspace_id or default_webspace_id()
        self.caps.require("core", "scenarios.manage")
        ui_section, registry_section, catalog_section, data_section = self._ensure_yjs_payload(scenario_id, space=space)

        async with _scenario_manager_async_write_meta():
            async with async_get_ydoc(target_webspace) as ydoc:
                self._project_to_doc(ydoc, scenario_id, ui_section, registry_section, catalog_section, data_section)

        if emit_event:
            emit(self.bus, "scenarios.synced", {"scenario_id": scenario_id, "webspace_id": target_webspace}, "scenario.mgr")

    def _post_install_bootstrap(self, scenario_id: str, webspace_id: str | None = None) -> dict[str, Any]:
        """
        Stage A2: after a scenario is installed into the workspace repo,
        apply its manifest-defined dependencies by installing/activating
        skills via the existing SkillManager.
        """
        target_webspace = webspace_id or default_webspace_id()
        result: dict[str, Any] = {
            "ok": True,
            "scenario_id": scenario_id,
            "webspace_id": target_webspace,
            "required": [],
            "items": [],
            "succeeded": [],
            "failed": [],
        }
        manifest = read_manifest(scenario_id)
        bindings = parse_scenario_skill_bindings(manifest if isinstance(manifest, dict) else {})
        depends = list(bindings.required)
        result["required"] = [dep for dep in depends if isinstance(dep, str) and dep]
        if not depends:
            return result

        # Use the same construction pattern as CLI SkillManager.
        skill_reg = SqliteSkillRegistry(self.ctx.sql)
        skill_mgr = SkillManager(
            repo=self.ctx.skills_repo,
            registry=skill_reg,
            git=self.ctx.git,
            paths=self.ctx.paths,
            bus=self.bus,
            caps=self.ctx.caps,
        )
        for dep in depends:
            if not isinstance(dep, str) or not dep:
                continue
            item: dict[str, Any] = {
                "name": dep,
                "required": True,
                "installed": False,
                "prepared": False,
                "activated": False,
                "ok": False,
            }
            try:
                # Ensure installed in monorepo and then activate runtime.
                skill_mgr.install(dep)
                item["installed"] = True
                runtime = skill_mgr.prepare_runtime(dep, run_tests=False)
                item["prepared"] = True
                version = getattr(runtime, "version", None)
                slot = getattr(runtime, "slot", None)
                item["version"] = version
                item["slot"] = slot
                skill_mgr.activate_for_space(dep, version=version, slot=slot, space="default", webspace_id=target_webspace)
                item["activated"] = True
                item["ok"] = True
                result["succeeded"].append(dep)
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
                result["failed"].append(dep)
                result["ok"] = False
            result["items"].append(item)
        try:
            emit(
                self.bus,
                "scenario.dependencies.bootstrapped",
                {
                    "scenario_id": scenario_id,
                    "webspace_id": target_webspace,
                    "ok": bool(result.get("ok")),
                    "succeeded": list(result.get("succeeded") or []),
                    "failed": list(result.get("failed") or []),
                },
                "scenario.mgr",
            )
        except Exception:
            pass
        return result

    def remove(self, name: str, *, safe: bool = False) -> None:
        self.caps.require("core", "scenarios.manage", "net.git")
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid scenario name")
        self.repo.ensure()
        self.reg.unregister(name)
        root = self.ctx.paths.workspace_dir()
        names = [r.name for r in self.reg.list()]
        prefixed = [f"scenarios/{n}" for n in names]
        # в тестах/без .git — только реестр, без git операций
        test_mode = os.getenv("ADAOS_TESTING") == "1"
        if test_mode or not (root / ".git").exists():
            return f"uninstalled: {name} (registry-only{' test-mode' if test_mode else ''})"
        self.git.sparse_init(str(root), cone=False)
        if prefixed:
            self.git.sparse_set(str(root), prefixed, no_cone=True)
        if safe:
            # Безопасный режим: синхронизируем workspace с удалённым репо.
            self.git.pull(str(root))
        remove_tree(
            str(root / "scenarios" / name),
            fs=self.paths.ctx.fs if hasattr(self.paths, "ctx") else get_ctx().fs,
        )
        emit(self.bus, "scenario.removed", {"id": name}, "scenario.mgr")
        try:
            uninstall_scenario_from_capacity(name)
            try:
                conf = load_config()
                if conf.role == "hub":
                    cap = get_local_capacity()
                    get_directory().repo.replace_scenario_capacity(conf.node_id, cap.get("scenarios") or [])
            except Exception:
                pass
        except Exception:
            pass

    def push(self, name: str, message: str, *, signoff: bool = False, bump: bool = True) -> str:
        self.caps.require("core", "scenarios.manage", "git.write", "net.git")
        root = self.ctx.paths.workspace_dir()
        try:
            from adaos.services.git.availability import get_git_availability

            av = get_git_availability(base_dir=self.ctx.settings.base_dir)
            if not av.enabled:
                raise RuntimeError("Git is disabled/unavailable on this node. Run `adaos git enable` when git is installed.")
        except ImportError:
            pass
        if not (Path(root) / ".git").exists():
            raise RuntimeError("Scenarios repo is not initialized. Run `adaos scenario sync` once.")
        sub = name.strip()
        subpath = f"scenarios/{sub}"
        self._ensure_scenario_subpath_materialized(Path(root), sub)
        version = self._bump_scenario_manifest_patch(Path(root) / "scenarios" / sub) if bump else None
        upsert_workspace_registry_entry(Path(root), "scenarios", Path(root) / "scenarios" / sub)
        if version and getattr(self, "reg", None) is not None:
            try:
                self.reg.register(sub, active_version=version)
            except Exception:
                pass
        changed = sorted(
            {
                *self.git.changed_files(str(root), subpath=subpath),
                *self.git.changed_files(str(root), subpath="registry.json"),
            }
        )
        if not changed:
            return "nothing-to-push"
        bad = check_no_denied(changed)
        if bad:
            raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
        msg = sanitize_message(message)
        ctx = get_ctx()
        sha = self.git.commit_subpath(
            str(root),
            subpath=[subpath, "registry.json"],
            message=msg,
            author_name=ctx.settings.git_author_name,
            author_email=ctx.settings.git_author_email,
            signoff=signoff,
        )
        if sha != "nothing-to-commit":
            self.git.push(str(root))
        return sha

    def _ensure_scenario_subpath_materialized(self, root: Path, name: str) -> None:
        scenario_dir = root / "scenarios" / name
        has_manifest = any((scenario_dir / item).exists() for item in _SCENARIO_MANIFEST_NAMES)
        try:
            self.git.sparse_add(str(root), f"scenarios/{name}")
        except Exception:
            return
        if has_manifest:
            return
        try:
            self.git.pull(str(root))
        except Exception:
            pass
        try:
            wait_for_materialized(scenario_dir, files=_SCENARIO_MANIFEST_NAMES, attempts=5, delay=0.1)
        except FileNotFoundError:
            if not scenario_dir.exists():
                raise FileNotFoundError(
                    f"scenario '{name}' is not materialized in workspace sparse checkout"
                ) from None

    def _bump_scenario_manifest_patch(self, scenario_dir: Path) -> str | None:
        candidates = ("scenario.yaml", "scenario.yml", "scenario.json")
        manifest_path: Path | None = None
        for candidate in candidates:
            path = scenario_dir / candidate
            if path.exists():
                manifest_path = path
                break
        if manifest_path is None:
            return None

        try:
            if manifest_path.suffix.lower() == ".json":
                payload = json.loads(manifest_path.read_text(encoding="utf-8")) or {}
            else:
                payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        existing = payload.get("version")
        existing_version = existing if isinstance(existing, str) and existing.strip() else None
        payload["version"] = bump_version(existing_version, 2)
        payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        if manifest_path.suffix.lower() == ".json":
            manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            manifest_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False) + "\n", encoding="utf-8")
        return str(payload.get("version") or "")

    def publish(self, name: str, *, bump: Literal["major", "minor", "patch"] = "patch", force: bool = False, dry_run: bool = False, signoff: bool = False) -> ArtifactPublishResult:
        """
        Полный publish: локальная сборка версии (dev→workspace) + git commit/push подпути в workspace репо.
        """
        result = self._publish_artifact(
            self.ctx.config,  # или self._load_config() если нужно
            "skills",
            name,
            bump=bump,
            force=force,
            dry_run=dry_run,
        )
        if result.dry_run:
            return result
        # гарантируем, что подпуть есть в sparse-checkout (на случай узкой sparse-конфигурации)
        try:
            self.ctx.git.sparse_add(str(self.ctx.paths.workspace_dir()), f"skills/{name}")
        except Exception:
            pass
        # коммитим и пушим изменения только своего подпути
        msg = f"publish(skill): {result.name} v{result.version}"
        sha = self.push(result.name, msg, signoff=signoff, bump=False)
        # ничего не мешает вернуть sha в result через setattr/обновлённый датакласс — но это опционально
        return result

    def uninstall(self, name: str, *, safe: bool = False) -> None:
        """Alias for :meth:`remove` to keep parity with :class:`SkillManager`."""
        self.remove(name, safe=safe)
