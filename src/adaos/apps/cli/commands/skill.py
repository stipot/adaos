# src\adaos\apps\cli\commands\skill.py
from __future__ import annotations

import json
import os
import subprocess
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import typer
import requests

from adaos.sdk.data.i18n import _
from adaos.apps.cli.active_control import probe_control_api, resolve_control_base_url, resolve_control_token
from adaos.apps.cli.git_status import (
    compute_path_status,
    ensure_remote,
    fetch_remote,
    list_changed_paths,
    ref_exists,
    render_diff,
    resolve_base_ref,
    read_path_divergence,
    render_noindex_diff,
    unzip_b64_to_dir,
)
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.root.client import RootHttpClient
from adaos.services.root.service import create_zip_bytes
from adaos.services.skill.manager import RuntimeInstallResult, SkillManager
from adaos.services.skill.runtime import (
    SkillRuntimeError,
    run_skill_handler_sync,
)
from adaos.services.skill.update import SkillUpdateService
from adaos.services.skill.validation import SkillValidationService
from adaos.services.skill.scaffold import create as scaffold_create
from adaos.services.runtime_refresh import rebuild_webspace_projection_sync, refresh_skill_runtime
from adaos.services.workspace_registry import build_registry_entry, list_workspace_registry_entries
from adaos.adapters.db import SqliteSkillRegistry
from adaos.services.eventbus import emit as bus_emit
from adaos.services.yjs.webspace import default_webspace_id

app = typer.Typer(help=_("cli.help_skill"))
service_app = typer.Typer(help="Manage service-type skills (start/stop/restart/status).")
app.add_typer(service_app, name="service")


def _workspace_child_names(root: Path) -> list[str]:
    if not root.exists():
        return []
    names: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith((".", "_")):
            continue
        names.append(child.name)
    return sorted(set(names))


def _repo_workspace_skills_root(ctx) -> Path | None:
    try:
        repo_root_attr = getattr(ctx.paths, "repo_root", None)
        repo_root = repo_root_attr() if callable(repo_root_attr) else repo_root_attr
        if not repo_root:
            return None
        candidate = Path(repo_root).expanduser().resolve() / ".adaos" / "workspace" / "skills"
        if candidate.exists():
            return candidate
    except Exception:
        return None
    return None


def _collect_workspace_skill_names(ctx, workspace_skills_root: Path) -> list[str]:
    names = set(_workspace_child_names(workspace_skills_root))
    repo_root = _repo_workspace_skills_root(ctx)
    if repo_root is not None:
        names.update(_workspace_child_names(repo_root))
    return sorted(names)


def _collect_runtime_skill_names(workspace_skills_root: Path) -> list[str]:
    runtime_root = workspace_skills_root / ".runtime"
    if not runtime_root.exists():
        return []
    names: list[str] = []
    for child in runtime_root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith((".", "_")):
            continue
        names.append(child.name)
    return sorted(set(names))


def _resolve_workspace_skill_source(ctx, skill_name: str, workspace_root: Path, workspace_skills_root: Path) -> tuple[Path, Path, str]:
    local_path = (workspace_skills_root / skill_name).resolve()
    if local_path.exists():
        return workspace_root, local_path, "workspace"

    repo_root = _repo_workspace_skills_root(ctx)
    if repo_root is not None:
        repo_path = (repo_root / skill_name).resolve()
        if repo_path.exists():
            repo_workdir_attr = getattr(ctx.paths, "repo_root", None)
            repo_workdir = repo_workdir_attr() if callable(repo_workdir_attr) else repo_workdir_attr
            workdir = Path(repo_workdir).expanduser().resolve() if repo_workdir else repo_root.parent.parent.parent.resolve()
            return workdir, repo_path, "repo_workspace_fallback"

    return workspace_root, local_path, "workspace"


def _normalize_runtime_missing_state(skill_name: str) -> dict[str, object]:
    return {
        "name": skill_name,
        "installed": False,
        "ready": False,
        "state": "runtime-missing",
    }


def _clean_version_text(value: object | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _read_local_artifact_version(kind: str, artifact_dir: Path) -> str | None:
    try:
        entry = build_registry_entry(kind, artifact_dir)
    except Exception:
        entry = None
    if not isinstance(entry, dict):
        return None
    return _clean_version_text(entry.get("version"))


def _resolve_list_skill_version(
    *,
    ctx,
    skill_name: str,
    row_version: object | None,
    workspace_root: Path,
    workspace_skills_root: Path,
    registry_meta: dict[str, object] | None,
) -> str:
    _source_workdir, source_path, _source_kind = _resolve_workspace_skill_source(
        ctx,
        skill_name,
        workspace_root,
        workspace_skills_root,
    )
    workspace_version = _read_local_artifact_version("skills", source_path)
    if not workspace_version and isinstance(registry_meta, dict):
        workspace_version = _clean_version_text(registry_meta.get("version"))
    return workspace_version or _clean_version_text(row_version) or "unknown"


def _resolve_list_skill_git_flags(
    *,
    ctx,
    skill_name: str,
    workspace_root: Path,
    workspace_skills_root: Path,
) -> list[str]:
    source_workdir, source_path, source_kind = _resolve_workspace_skill_source(
        ctx,
        skill_name,
        workspace_root,
        workspace_skills_root,
    )
    base_ref = resolve_base_ref(source_workdir) if source_kind in {"workspace", "repo_workspace_fallback"} else None
    try:
        path_status = compute_path_status(
            workdir=source_workdir,
            path=source_path,
            base_ref=base_ref,
        )
    except Exception:
        return []
    return _git_path_flags(path_status)


def _status_value(status: object, key: str, default: object = None) -> object:
    if isinstance(status, dict):
        return status.get(key, default)
    return getattr(status, key, default)


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _git_path_flags(status: object) -> list[str]:
    flags: list[str] = []
    error = str(_status_value(status, "error", "") or "").strip()
    if error:
        flags.append("git-error")
    dirty = bool(_status_value(status, "dirty", False))
    changed = bool(_status_value(status, "changed_vs_base", False))
    ahead = _positive_int(_status_value(status, "ahead_count", 0))
    behind = _positive_int(_status_value(status, "behind_count", 0))
    if dirty:
        flags.append("git-dirty")
    if changed and ahead:
        flags.append("git-ahead")
    if changed and behind:
        flags.append("git-behind")
    if changed and not ahead and not behind:
        flags.append("git-different")
    return flags


def _compare_versions(left: str | None, right: str | None) -> int | None:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return None
    try:
        from packaging.version import Version

        left_version = Version(left_text)
        right_version = Version(right_text)
    except Exception:
        left_parts = _simple_version_parts(left_text)
        right_parts = _simple_version_parts(right_text)
        if left_parts is None or right_parts is None:
            return None
        max_len = max(len(left_parts), len(right_parts))
        left_parts = left_parts + (0,) * (max_len - len(left_parts))
        right_parts = right_parts + (0,) * (max_len - len(right_parts))
        if left_parts > right_parts:
            return 1
        if left_parts < right_parts:
            return -1
        return 0
    if left_version > right_version:
        return 1
    if left_version < right_version:
        return -1
    return 0


def _simple_version_parts(value: str) -> tuple[int, ...] | None:
    text = str(value or "").strip().removeprefix("v").removeprefix("V")
    if not text:
        return None
    parts: list[int] = []
    for segment in text.split("."):
        digits: list[str] = []
        for char in segment:
            if not char.isdigit():
                break
            digits.append(char)
        if not digits:
            return None
        parts.append(int("".join(digits)))
    return tuple(parts)


def _runtime_version_flags(
    workspace_version: str | None,
    runtime_version: str | None,
    version_drift: bool,
) -> list[str]:
    if not version_drift:
        return []
    order = _compare_versions(workspace_version, runtime_version)
    if order is None or order == 0:
        return ["runtime-different"]
    if order > 0:
        return ["runtime-behind"]
    return ["runtime-ahead"]


def _resolve_workspace_skill_versions(
    *,
    runtime_state: dict[str, object] | None,
    registry_meta: dict[str, object] | None,
    source_path: Path,
) -> tuple[str | None, str | None, bool]:
    workspace_version = _read_local_artifact_version("skills", source_path)
    if not workspace_version and isinstance(registry_meta, dict):
        workspace_version = _clean_version_text(registry_meta.get("version"))
    runtime_version = None
    if isinstance(runtime_state, dict):
        runtime_version = _clean_version_text(runtime_state.get("version"))
    version_drift = False
    if workspace_version and runtime_version:
        order = _compare_versions(workspace_version, runtime_version)
        version_drift = (workspace_version != runtime_version) if order is None else order != 0
    return workspace_version, runtime_version, version_drift


def _resolve_list_skill_flags(
    *,
    ctx,
    skill_name: str,
    row_version: object | None,
    runtime_state: dict[str, object] | None = None,
    workspace_root: Path,
    workspace_skills_root: Path,
    registry_meta: dict[str, object] | None,
) -> list[str]:
    _source_workdir, source_path, _source_kind = _resolve_workspace_skill_source(
        ctx,
        skill_name,
        workspace_root,
        workspace_skills_root,
    )
    if runtime_state is None and _clean_version_text(row_version):
        runtime_state = {"version": row_version}
    workspace_version, runtime_version, version_drift = _resolve_workspace_skill_versions(
        runtime_state=runtime_state,
        registry_meta=registry_meta,
        source_path=source_path,
    )
    return [
        *_runtime_version_flags(workspace_version, runtime_version, version_drift),
        *_resolve_list_skill_git_flags(
            ctx=ctx,
            skill_name=skill_name,
            workspace_root=workspace_root,
            workspace_skills_root=workspace_skills_root,
        ),
    ]


def _skill_names_from_paths(paths: list[str]) -> list[str]:
    names: set[str] = set()
    for path in paths:
        parts = str(path or "").replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] == "skills" and parts[1]:
            names.add(parts[1])
    return sorted(names)


def _default_skill_release_message(skill_name: str) -> str:
    safe_name = str(skill_name or "skill").strip() or "skill"
    return f"chore({safe_name}): release workspace changes"


def _registry_release_reasons(
    *,
    source_path: Path,
    registry_meta: dict[str, object] | None,
) -> list[str]:
    reasons: list[str] = []
    workspace_version = _read_local_artifact_version("skills", source_path)
    registry_version = _clean_version_text((registry_meta or {}).get("version") if isinstance(registry_meta, dict) else None)
    if workspace_version:
        if not registry_version:
            reasons.append("registry-missing")
        elif workspace_version != registry_version:
            reasons.append("registry-version")
    return reasons


def _collect_skill_release_candidates(
    *,
    skill_name: str | None = None,
    remote: str = "origin",
) -> dict[str, object]:
    ctx = get_ctx()
    workspace_root = Path(ctx.paths.workspace_dir())
    if not (workspace_root / ".git").exists():
        raise RuntimeError("Skills workspace repo is not initialized. Run `adaos skill sync` once.")

    mgr = _mgr()
    caps = getattr(mgr, "caps", None)
    if caps is not None:
        caps.require("core", "skills.manage", "git.write", "net.git")

    if skill_name:
        _resolve_skill_path(skill_name)

    base_ref = resolve_base_ref(workspace_root, remote=remote)
    if base_ref and not ref_exists(workspace_root, base_ref):
        base_ref = None

    ahead: int | None = 0
    behind: int | None = 0
    if base_ref:
        ahead, behind = read_path_divergence(workspace_root, base_ref=base_ref, path="skills")
    ahead_count = _positive_int(ahead)
    behind_count = _positive_int(behind)
    reasons_by_skill: dict[str, set[str]] = {}

    if base_ref:
        changed_paths = list_changed_paths(workspace_root, base_ref=base_ref, path="skills")
        for name in _skill_names_from_paths(changed_paths):
            reasons_by_skill.setdefault(name, set()).add("git-ahead")

    try:
        dirty_paths = list(ctx.git.changed_files(str(workspace_root), subpath="skills"))
    except Exception:
        dirty_paths = []
    for name in _skill_names_from_paths(dirty_paths):
        reasons_by_skill.setdefault(name, set()).add("git-dirty")

    workspace_skills_root = workspace_root / "skills"
    registry_by_name: dict[str, dict[str, object]] = {}
    try:
        registry_items = list_workspace_registry_entries(workspace_root, kind="skills", fallback_to_scan=True)
    except Exception:
        registry_items = []
    for item in registry_items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or item.get("id") or "").strip()
        if item_name:
            registry_by_name[item_name] = item

    release_names = set(registry_by_name)
    release_names.update(_workspace_child_names(workspace_skills_root))
    if skill_name:
        release_names = {skill_name}
    for name in sorted(release_names):
        source_path = workspace_skills_root / name
        if not source_path.exists():
            continue
        for reason in _registry_release_reasons(
            source_path=source_path,
            registry_meta=registry_by_name.get(name),
        ):
            reasons_by_skill.setdefault(name, set()).add(reason)

    if skill_name:
        reasons_by_skill = {skill_name: reasons for name, reasons in reasons_by_skill.items() if name == skill_name}

    candidates = [
        {"name": name, "reasons": sorted(reasons)}
        for name, reasons in sorted(reasons_by_skill.items())
        if reasons
    ]
    return {
        "base_ref": base_ref,
        "ahead_count": ahead_count,
        "behind_count": behind_count,
        "skills": candidates,
    }


def _release_changed_skills(
    *,
    skill_name: str | None = None,
    remote: str = "origin",
    signoff: bool = False,
) -> dict[str, object]:
    candidates = _collect_skill_release_candidates(skill_name=skill_name, remote=remote)
    candidate_items = [item for item in candidates.get("skills") or [] if isinstance(item, dict)]

    if skill_name and not candidate_items:
        return {
            "pushed": False,
            "reason": "skill-no-release-changes",
            **candidates,
        }
    if not candidate_items:
        return {
            "pushed": False,
            "reason": "nothing-to-release",
            **candidates,
        }

    mgr = _mgr()
    released: list[dict[str, object]] = []
    for item in candidate_items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        message = _default_skill_release_message(name)
        revision = mgr.push(name, message, signoff=signoff)
        released.append(
            {
                "name": name,
                "revision": revision,
                "message": message,
                "reasons": list(item.get("reasons") or []),
            }
        )

    return {
        "pushed": True,
        **candidates,
        "released": released,
    }


def _resolve_skill_display_version(
    *,
    space: str,
    runtime_state: dict[str, object] | None,
    workspace_version: str | None,
    runtime_version: str | None,
) -> str:
    if workspace_version:
        return workspace_version
    if runtime_version:
        return runtime_version
    state = str((runtime_state or {}).get("state") or "").strip()
    if space == "dev" or (runtime_state or {}).get("installed") is False or state in {"draft", "runtime-missing"}:
        return "n/a"
    return "unknown"


def _run_safe(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _mgr() -> SkillManager:
    ctx = get_ctx()
    repo = ctx.skills_repo
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=getattr(ctx, "bus", None), caps=ctx.caps)


def _hub_base_url() -> str:
    # Skill operations are node-local even on member nodes, so prefer the
    # local control API over the durable member->hub rendezvous URL.
    return resolve_control_base_url(prefer_local=True)


def _hub_api_ready(*, timeout_s: float = 2.0) -> bool:
    base = _hub_base_url()
    token = str(_hub_headers().get("X-AdaOS-Token") or "")
    code, payload = probe_control_api(base_url=base, token=token, timeout_s=timeout_s)
    if code is None:
        return False
    if int(code) != 200:
        return False
    return isinstance(payload, dict)


def _hub_headers(*, base_url: str | None = None) -> dict[str, str]:
    resolved_base = str(base_url or _hub_base_url() or "").strip() or None
    try:
        token = str(resolve_control_token(base_url=resolved_base))
    except TypeError:
        # Some tests still patch the older signature without base_url support.
        token = str(resolve_control_token())
    return {"X-AdaOS-Token": token}


def _hub_get(path: str, *, params: dict | None = None) -> dict:
    base = _hub_base_url()
    url = base + path
    resp = requests.get(url, headers=_hub_headers(base_url=base), params=params or {}, timeout=10)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("error") or payload.get("message") or payload)
            else:
                detail = str(payload)
        except Exception:
            detail = (resp.text or "").strip()
        raise RuntimeError(f"HTTP {resp.status_code} GET {path}: {detail}".strip()) from exc
    return resp.json()


def _hub_post(path: str, *, body: dict | None = None, timeout_s: float = 30) -> dict:
    base = _hub_base_url()
    url = base + path
    resp = requests.post(url, headers=_hub_headers(base_url=base), json=body or {}, timeout=timeout_s)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("error") or payload.get("message") or payload)
            else:
                detail = str(payload)
        except Exception:
            detail = (resp.text or "").strip()
        raise RuntimeError(f"HTTP {resp.status_code} POST {path}: {detail}".strip()) from exc
    return resp.json()


def _notify_hub_skill_activated(
    name: str,
    *,
    space: str = "default",
    webspace_id: str | None = None,
    defer_webspace_rebuild: bool = False,
) -> None:
    try:
        _hub_post(
            "/api/skills/runtime/notify-activated",
            body={
                "name": name,
                "space": space,
                "webspace_id": webspace_id or default_webspace_id(),
                "defer_webspace_rebuild": bool(defer_webspace_rebuild),
            },
        )
    except Exception:
        pass


def _emit_local_skill_updated(name: str, *, webspace_id: str | None = None) -> None:
    try:
        ctx = get_ctx()
        bus = getattr(ctx, "bus", None)
        if bus is None:
            return
        bus_emit(
            bus,
            "skills.updated",
            {
                "name": name,
                "webspace_id": webspace_id or default_webspace_id(),
            },
            "cli.skill",
        )
    except Exception:
        pass


def _rebuild_local_webspace(*, webspace_id: str | None = None) -> None:
    try:
        rebuild_webspace_projection_sync(
            webspace_id=webspace_id or default_webspace_id(),
            action="cli_skill_runtime_sync",
            source_of_truth="skill_runtime",
        )
    except Exception:
        pass


def _rebuild_hub_webspace(*, webspace_id: str | None = None) -> None:
    _hub_post(
        "/api/skills/runtime/rebuild-webspace",
        body={"webspace_id": webspace_id or default_webspace_id()},
        timeout_s=120,
    )


def _refresh_runtime_side_effects(
    name: str,
    *,
    webspace_id: str | None = None,
    notify_activation: bool = False,
    emit_updated: bool = False,
    defer_hub_rebuild: bool = False,
    rebuild_local: bool = True,
) -> None:
    target_webspace = webspace_id or default_webspace_id()
    if emit_updated:
        _emit_local_skill_updated(name, webspace_id=target_webspace)
    if notify_activation:
        _notify_hub_skill_activated(
            name,
            webspace_id=target_webspace,
            defer_webspace_rebuild=defer_hub_rebuild,
        )
    if rebuild_local:
        _rebuild_local_webspace(webspace_id=target_webspace)


@_run_safe
@service_app.command("list")
def service_list(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    check_health: bool = typer.Option(False, "--health", help="Also call each service /health endpoint."),
):
    data = _hub_get("/api/services", params={"check_health": check_health})
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False))
        return
    services = data.get("services") or []
    if not services:
        typer.echo("no service skills discovered")
        return
    for s in services:
        if not isinstance(s, dict):
            continue
        name = s.get("name") or "<unknown>"
        running = "running" if s.get("running") else "stopped"
        base = s.get("base_url") or ""
        extra = ""
        if check_health and "health_ok" in s:
            extra = " health=ok" if s.get("health_ok") else " health=fail"
        typer.echo(f"{name}: {running} {base}{extra}")


@_run_safe
@service_app.command("status")
def service_status(
    name: str = typer.Argument(..., help="Service skill name (folder name in skills workspace)."),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    check_health: bool = typer.Option(False, "--health", help="Also call the service /health endpoint."),
):
    data = _hub_get(f"/api/services/{name}", params={"check_health": check_health})
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False))
        return
    svc = data.get("service") or {}
    typer.echo(json.dumps(svc, ensure_ascii=False, indent=2))


@_run_safe
@service_app.command("start")
def service_start(name: str = typer.Argument(..., help="Service skill name.")):
    _hub_post(f"/api/services/{name}/start")
    typer.secho(f"started {name}", fg=typer.colors.GREEN)


@_run_safe
@service_app.command("stop")
def service_stop(name: str = typer.Argument(..., help="Service skill name.")):
    _hub_post(f"/api/services/{name}/stop")
    typer.secho(f"stopped {name}", fg=typer.colors.GREEN)


@_run_safe
@service_app.command("restart")
def service_restart(name: str = typer.Argument(..., help="Service skill name.")):
    _hub_post(f"/api/services/{name}/restart")
    typer.secho(f"restarted {name}", fg=typer.colors.GREEN)


def _ensure_workspace_gitignore(workspace: Path) -> None:
    """Ensure that the shared workspace has a .gitignore with runtime exclusions."""

    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / ".gitignore"
    if target.exists():
        return

    entries = [
        "skills/.runtime",
        "skills/.devtime",
        "scenario/.runtime",
        "scenario/.devtime",
    ]
    target.write_text("\n".join(entries) + "\n", encoding="utf-8")


def _workspace_root() -> Path:
    ctx = get_ctx()
    attr = getattr(ctx.paths, "skills_workspace_dir", None)
    if attr is not None:
        value = attr() if callable(attr) else attr
    else:
        base = getattr(ctx.paths, "skills_dir")
        value = base() if callable(base) else base
    root = Path(value).expanduser().resolve()
    workspace = root.parent if root.name.lower() == "skills" else root
    _ensure_workspace_gitignore(workspace)
    return root


def _resolve_skill_path(target: str) -> Path:
    candidate = Path(target).expanduser()
    if candidate.exists():
        return candidate.resolve()
    root = _workspace_root()
    candidate = (root / target).resolve()
    if candidate.exists():
        return candidate
    raise typer.BadParameter(_("cli.skill.push.not_found", name=target))


def _list_changed_workspace_skills() -> list[str]:
    workspace = _workspace_root()
    proc = subprocess.run(
        ["git", "-C", str(workspace.parent if workspace.name.lower() == "skills" else workspace), "status", "--porcelain", "--", "skills"],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip() or "git status failed"
        raise RuntimeError(message)

    changed: set[str] = set()
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if not path_text:
            continue
        candidates = [segment.strip() for segment in path_text.split("->") if segment.strip()]
        for candidate in candidates:
            normalized = candidate.replace("\\", "/")
            if not normalized.startswith("skills/"):
                continue
            parts = normalized.split("/")
            if len(parts) < 2:
                continue
            skill_name = (parts[1] or "").strip()
            if skill_name and skill_name != ".runtime" and skill_name != ".devtime":
                changed.add(skill_name)
    return sorted(changed)


def _list_migratable_workspace_skills(*, ctx, mgr: SkillManager) -> list[str]:
    workspace_root = Path(ctx.paths.workspace_dir())
    workspace_skills_root = Path(ctx.paths.skills_workspace_dir())

    workspace_registry_by_name: dict[str, dict[str, object]] = {}
    try:
        registry_items = list_workspace_registry_entries(workspace_root, kind="skills", fallback_to_scan=True)
    except Exception:
        registry_items = []
    for item in registry_items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or item.get("id") or "").strip()
        if item_name:
            workspace_registry_by_name[item_name] = item

    try:
        changed = set(_list_changed_workspace_skills())
    except Exception:
        changed = set()

    installed_names: set[str] = set()
    try:
        rows = SqliteSkillRegistry(ctx.sql).list()
    except Exception:
        rows = []
    for row in rows:
        if not bool(getattr(row, "installed", True)):
            continue
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if name:
            installed_names.add(str(name))

    names = sorted(installed_names | set(_collect_runtime_skill_names(workspace_skills_root)) | changed)
    candidates = set(changed)
    for skill_name in names:
        _source_workdir, source_path, _source_kind = _resolve_workspace_skill_source(
            ctx,
            skill_name,
            workspace_root,
            workspace_skills_root,
        )
        if not source_path.exists() and skill_name not in changed:
            continue

        runtime_state: dict[str, object] | None = None
        try:
            runtime_state = mgr.runtime_status(skill_name)
        except Exception as exc:
            message = str(exc or "").strip()
            if message.lower() == "no versions installed":
                runtime_state = _normalize_runtime_missing_state(skill_name)

        workspace_version, runtime_version, version_drift = _resolve_workspace_skill_versions(
            runtime_state=runtime_state,
            registry_meta=workspace_registry_by_name.get(skill_name),
            source_path=source_path,
        )
        runtime_state_name = str((runtime_state or {}).get("state") or "").strip().lower()
        runtime_missing = runtime_state_name == "runtime-missing" or (
            bool(workspace_version) and not bool(runtime_version)
        )
        if version_drift or runtime_missing:
            candidates.add(skill_name)

    return sorted(candidates)


def _echo_runtime_install(result: RuntimeInstallResult) -> None:
    typer.secho(
        f"installed {result.name} v{result.version} into slot {result.slot}",
        fg=typer.colors.GREEN,
    )
    if result.tests:
        summary = ", ".join(f"{name}={out.status}" for name, out in result.tests.items())
        typer.echo(f"tests: {summary}")
    typer.echo(f"resolved manifest: {result.resolved_manifest}")


@_run_safe
@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    show_fs: bool = typer.Option(False, "--fs", help=_("cli.option.fs")),
    local: bool = typer.Option(False, "--local", help="Force local execution (bypass hub API)."),
):
    """
    Список установленных навыков из реестра.
    JSON-формат: {"skills": [{"name": "...", "version": "..."}, ...]}
    """
    if not local and _hub_api_ready():
        data = _hub_get("/api/skills/list", params={"fs": bool(show_fs)})
        items = data.get("items") or []
        if json_output:
            payload = {
                "skills": [
                    {
                        "name": (r.get("name") or r.get("id") or r.get("repr") or ""),
                        "version": (r.get("active_version") or r.get("version") or "unknown"),
                    }
                    for r in items
                    if isinstance(r, dict)
                ]
            }
            typer.echo(json.dumps(payload, ensure_ascii=False))
            return
        if not items:
            typer.echo(_("skill.list.empty"))
        else:
            for r in items:
                if not isinstance(r, dict):
                    continue
                name = (r.get("name") or r.get("id") or r.get("repr") or "").strip()
                ver = (r.get("active_version") or r.get("version") or "unknown")
                if name:
                    typer.echo(_("cli.skill.list.item", name=name, version=ver))
        if show_fs and isinstance(data.get("fs"), dict):
            fs = data.get("fs") or {}
            missing = fs.get("missing") or []
            extra = fs.get("extra") or []
            if missing:
                typer.echo(_("cli.skill.fs_missing", items=", ".join(sorted(str(x) for x in missing))))
            if extra:
                typer.echo(_("cli.skill.fs_extra", items=", ".join(sorted(str(x) for x in extra))))
        return

    mgr = _mgr()
    rows = mgr.list_installed()  # SkillRecord[]
    ctx = get_ctx()
    workspace_root = Path(ctx.paths.workspace_dir())
    workspace_skills_root = Path(ctx.paths.skills_workspace_dir())
    workspace_registry_by_name: dict[str, dict[str, object]] = {}
    try:
        registry_items = list_workspace_registry_entries(workspace_root, kind="skills", fallback_to_scan=True)
    except Exception:
        registry_items = []
    for item in registry_items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or item.get("id") or "").strip()
        if item_name:
            workspace_registry_by_name[item_name] = item

    def _list_runtime_state(skill_name: str, row_version: object | None) -> dict[str, object] | None:
        try:
            state = mgr.runtime_status(skill_name)
        except Exception:
            state = None
        if isinstance(state, dict):
            return state
        if _clean_version_text(row_version):
            return {"version": row_version}
        return None

    if json_output:
        payload = {
            "skills": [
                {
                    "name": r.name,
                    # тестам важен только name, но version полезно оставить
                    "version": _resolve_list_skill_version(
                        ctx=ctx,
                        skill_name=r.name,
                        row_version=getattr(r, "active_version", None),
                        workspace_root=workspace_root,
                        workspace_skills_root=workspace_skills_root,
                        registry_meta=workspace_registry_by_name.get(r.name),
                    ),
                    "flags": _resolve_list_skill_flags(
                        ctx=ctx,
                        skill_name=r.name,
                        row_version=getattr(r, "active_version", None),
                        runtime_state=_list_runtime_state(r.name, getattr(r, "active_version", None)),
                        workspace_root=workspace_root,
                        workspace_skills_root=workspace_skills_root,
                        registry_meta=workspace_registry_by_name.get(r.name),
                    ),
                }
                for r in rows
                # оставляем только действительно установленные (если поле есть)
                if bool(getattr(r, "installed", True))
            ]
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    if not rows:
        typer.echo(_("skill.list.empty"))
    else:
        for r in rows:
            if not bool(getattr(r, "installed", True)):
                continue
            av = _resolve_list_skill_version(
                ctx=ctx,
                skill_name=r.name,
                row_version=getattr(r, "active_version", None),
                workspace_root=workspace_root,
                workspace_skills_root=workspace_skills_root,
                registry_meta=workspace_registry_by_name.get(r.name),
            )
            flags = _resolve_list_skill_flags(
                ctx=ctx,
                skill_name=r.name,
                row_version=getattr(r, "active_version", None),
                runtime_state=_list_runtime_state(r.name, getattr(r, "active_version", None)),
                workspace_root=workspace_root,
                workspace_skills_root=workspace_skills_root,
                registry_meta=workspace_registry_by_name.get(r.name),
            )
            suffix = f" [{' '.join(flags)}]" if flags else ""
            typer.echo(f'{_("cli.skill.list.item", name=r.name, version=av)}{suffix}')

    if show_fs:
        present = {m.id.value for m in mgr.list_present()}
        desired = {r.name for r in rows if bool(getattr(r, "installed", True))}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(_("cli.skill.fs_missing", items=", ".join(sorted(missing))))
        if extra:
            typer.echo(_("cli.skill.fs_extra", items=", ".join(sorted(extra))))


@_run_safe
@app.command("sync")
def sync():
    """Deprecated: use ``adaos skill migrate`` instead."""
    typer.secho(
        "'skill sync' is deprecated. Use 'adaos skill migrate' to refresh skills.",
        fg=typer.colors.YELLOW,
    )


@_run_safe
@app.command("uninstall")
def uninstall(
    name: str,
    safe: bool = typer.Option(False, "--safe", help=_("cli.skill.uninstall.option.safe")),
    force: bool = typer.Option(False, "--force", help=_("cli.skill.uninstall.option.force")),
    local: bool = typer.Option(False, "--local", help="Force local execution (bypass hub API)."),
):
    if not local and _hub_api_ready():
        # Server-side uninstall is always the correct behavior for AB core slots.
        # API currently does not expose `safe`; keep it for backward-compat but ignore in remote mode.
        _hub_post(
            "/api/skills/uninstall",
            body={
                "name": name,
                "webspace_id": default_webspace_id(),
                "force": bool(force),
            },
        )
        typer.echo(_("cli.skill.uninstall.done", name=name))
        return
    mgr = _mgr()
    try:
        mgr.uninstall(name, safe=safe, force=force)
    except Exception as exc:
        message = str(exc)
        typer.secho(f"uninstall failed: {message}", fg=typer.colors.RED)
        lowered = message.lower()
        if "unstaged changes" in lowered and not force:
            typer.echo(_("cli.skill.uninstall.force_hint", name=name))
        raise typer.Exit(1) from exc
    _rebuild_local_webspace(webspace_id=default_webspace_id())
    typer.echo(_("cli.skill.uninstall.done", name=name))


@_run_safe
@app.command("reconcile-fs-to-db")
def reconcile_fs_to_db():
    """Обходит {skills_dir} и проставляет installed=1 для найденных папок (кроме .git).
    Не трогает active_version/repo_url.
    """
    mgr = _mgr()
    ctx = get_ctx()
    root = ctx.paths.skills_dir()
    if not root.exists():
        typer.echo(_("cli.skill.reconcile.missing_root"))
        raise typer.Exit(1)
    found = []
    for name in os.listdir(root):
        if name == ".git":
            continue
        p = root / name
        if p.is_dir():
            mgr.reg.register(name)  # installed=1
            found.append(name)
    typer.echo(
        _(
            "cli.skill.reconcile.added",
            items=", ".join(found) if found else _("cli.skill.reconcile.empty"),
        )
    )


@_run_safe
@app.command("push", context_settings={"allow_extra_args": True, "ignore_unknown_options": False})
def push_command(
    ctx: typer.Context,
    skill_name: Optional[str] = typer.Argument(None, help=_("cli.skill.push.name_help")),
    message: Optional[str] = typer.Option(None, "--message", "-m", help=_("cli.commit_message.help")),
    signoff: bool = typer.Option(False, "--signoff", help=_("cli.option.signoff")),
    remote: str = typer.Option("origin", "--remote", help="workspace git remote for release candidate comparison"),
):
    """
    Release workspace skill changes through manifest version bump, registry
    update, commit, and push.
    """
    extra = [str(item) for item in getattr(ctx, "args", []) or []]
    if extra:
        if any(part.startswith("-") for part in extra):
            raise typer.BadParameter(f"unexpected extra arguments: {' '.join(extra)}")
        message = " ".join([part for part in ([message] if message else []) + extra if str(part).strip()]).strip() or None

    if message is None:
        try:
            result = _release_changed_skills(skill_name=skill_name, remote=remote, signoff=signoff)
        except Exception as exc:
            typer.secho(f"push failed: {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        if not bool(result.get("pushed")):
            reason = str(result.get("reason") or "nothing-to-release")
            if reason == "skill-no-release-changes" and skill_name:
                typer.echo(f"skill {skill_name} has no release changes to push.")
            else:
                typer.echo("No skill release changes to push.")
            return
        released = [item for item in result.get("released") or [] if isinstance(item, dict)]
        skill_names = ", ".join(str(item.get("name") or "") for item in released if str(item.get("name") or "").strip()) or "(unknown)"
        base_ref = str(result.get("base_ref") or "(none)")
        ahead = _positive_int(result.get("ahead_count"))
        behind = _positive_int(result.get("behind_count"))
        typer.echo(f"released skill changes: {skill_names} (base={base_ref}, ahead={ahead}, behind={behind})")
        for item in released:
            name = str(item.get("name") or "").strip()
            revision = str(item.get("revision") or "").strip()
            reasons = ", ".join(str(reason) for reason in item.get("reasons") or [])
            suffix = f" [{reasons}]" if reasons else ""
            typer.echo(f"- {name}: {revision}{suffix}")
        return

    if not skill_name:
        typer.secho("skill name is required when --message/-m is used", fg=typer.colors.RED)
        raise typer.Exit(2)

    _resolve_skill_path(skill_name)
    mgr = _mgr()
    res = mgr.push(skill_name, message, signoff=signoff)
    if res in {"nothing-to-push", "nothing-to-commit"}:
        typer.echo(_("cli.skill.push.nothing"))
    else:
        typer.echo(_("cli.skill.push.done", name=skill_name, revision=res))


@_run_safe
@app.command("create")
def cmd_create(name: str, template: str = typer.Option("skill_default", "--template", "-t")):
    p = scaffold_create(name, template=template)
    typer.echo(_("cli.skill.create.created", path=p))
    typer.echo(_("cli.skill.create.hint_push", name=name))


@_run_safe
@app.command("scaffold")
def cmd_scaffold(name: str, template: str = typer.Option("skill_default", "--template", help="skill template name")):
    path = scaffold_create(name, template=template)
    typer.secho(f"scaffold created at {path}", fg=typer.colors.GREEN)


@_run_safe
@app.command("validate")
def cmd_validate(
    name: str,
    json_output: bool = typer.Option(False, "--json", help="machine readable output"),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="treat warnings as errors"),
    probe_tools: bool = typer.Option(False, "--probe-tools", help="import handlers to verify tool exports"),
):
    mgr = _mgr()
    try:
        report = mgr.validate_skill(name, strict=strict, probe_tools=probe_tools)
    except Exception as exc:
        typer.secho(f"validate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    issues = [asdict(issue) for issue in report.issues]
    if json_output:
        typer.echo(json.dumps({"ok": report.ok, "issues": issues}, ensure_ascii=False, indent=2))
        if not report.ok:
            raise typer.Exit(1)
        return

    if report.ok:
        typer.secho("validation passed", fg=typer.colors.GREEN)
        return

    for issue in report.issues:
        location = f" ({issue.where})" if issue.where else ""
        typer.echo(f"[{issue.level}] {issue.code}: {issue.message}{location}")
    raise typer.Exit(1)


@_run_safe
@app.command("install", help=_("cli.skill.install.help"))
def cmd_install(
    name: str,
    test: bool = typer.Option(False, "--test", help=_("cli.skill.install.option.test")),
    slot: Optional[str] = typer.Option(None, "--slot", help=_("cli.skill.install.option.slot")),
    silent: bool = typer.Option(False, "--silent", help=_("cli.skill.install.option.silent")),
    safe: bool = typer.Option(False, "--safe", help=_("cli.skill.install.option.safe")),
    local: bool = typer.Option(False, "--local", help="Force local execution (bypass hub API)."),
):
    if not local and _hub_api_ready(timeout_s=3.0):
        # API-first: install/prepare/activate via the running hub server (works even if repo root is stale vs active slot).
        try:
            installed = _hub_post(
                "/api/skills/install",
                body={
                    "name": name,
                    "pin": None,
                    "perform_validation": False,
                    "strict": False if safe else True,
                    "probe_tools": False,
                },
            )
        except Exception as exc:
            typer.secho(f"install failed (hub api): {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc

        skill_id = (
            ((installed.get("skill") or {}).get("id") if isinstance(installed, dict) else None)
            or str(name)
        )

        try:
            prep = _hub_post(
                "/api/skills/runtime/prepare",
                body={"name": skill_id, "run_tests": bool(test), "slot": (slot or None)},
            )
        except Exception as exc:
            typer.secho(f"runtime preparation failed (hub api): {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc

        try:
            activated = _hub_post(
                "/api/skills/runtime/activate",
                body={
                    "name": skill_id,
                    "slot": prep.get("slot") if isinstance(prep, dict) else None,
                    "version": prep.get("version") if isinstance(prep, dict) else None,
                    "auto_prepare": True,
                    "webspace_id": default_webspace_id(),
                },
            )
        except Exception as exc:
            typer.secho(f"activation failed (hub api): {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc

        slot_out = (activated.get("slot") if isinstance(activated, dict) else None) or (prep.get("slot") if isinstance(prep, dict) else None) or "?"
        typer.secho(f"skill {skill_id} now active on slot {slot_out}", fg=typer.colors.GREEN)

        if silent:
            return
        try:
            setup_result = _hub_post("/api/skills/runtime/setup", body={"name": skill_id})
        except RuntimeError as exc:
            message = str(exc)
            if "setup not supported" in message.lower():
                typer.secho(_("cli.skill.install.setup_not_supported"), fg=typer.colors.YELLOW)
                return
            typer.secho(_("cli.skill.install.setup_failed", error=message), fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        except Exception as exc:
            typer.secho(_("cli.skill.install.setup_failed", error=str(exc)), fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        if isinstance(setup_result, dict):
            if setup_result.get("ok") is False:
                detail = setup_result.get("error") or setup_result.get("message") or ""
                typer.secho(
                    _("cli.skill.install.setup_report_failed", detail=str(detail)),
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            detail = setup_result.get("message") or setup_result.get("detail")
            if detail:
                typer.echo(_("cli.skill.install.setup_success_with_detail", detail=str(detail)))
                return
        typer.echo(_("cli.skill.install.setup_success"))
        return

    mgr = _mgr()
    try:
        result = mgr.install(name, validate=False, safe=safe)
    except Exception as exc:
        message = str(exc)
        typer.secho(f"install failed: {message}", fg=typer.colors.RED)
        # Provide an explicit hint when Git reports unresolved merges.
        if "git pull" in message and "unmerged files" in message.lower():
            try:
                ctx = get_ctx()
                workspace_root = ctx.paths.workspace_dir()
                typer.echo(f"Skills workspace Git repo: {workspace_root}")
                typer.echo(
                    f"Run 'git -C \"{workspace_root}\" status' to inspect conflicted files, "
                    f"resolve them, then re-run 'adaos skill install {name}'."
                )
            except Exception:
                # Best-effort hint; ignore failures in helper diagnostics.
                pass
        raise typer.Exit(1) from exc

    if isinstance(result, tuple):
        meta, report = result
    elif hasattr(result, "id"):
        meta, report = result, None
    else:
        typer.echo(str(result))
        return

    if report is not None and hasattr(report, "ok") and not report.ok:
        typer.secho(str(report), fg=typer.colors.YELLOW)

    skill_name = meta.id.value if meta and hasattr(meta, "id") else name
    try:
        runtime = mgr.prepare_runtime(skill_name, run_tests=test, preferred_slot=slot)
    except Exception as exc:
        typer.secho(f"runtime preparation failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    _echo_runtime_install(runtime)

    try:
        activated_slot = mgr.activate_for_space(
            skill_name,
            version=runtime.version,
            slot=runtime.slot,
            space="default",
            webspace_id=default_webspace_id(),
        )
    except Exception as exc:
        typer.secho(f"activation failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.secho(f"skill {skill_name} now active on slot {activated_slot}", fg=typer.colors.GREEN)
    _refresh_runtime_side_effects(
        skill_name,
        webspace_id=default_webspace_id(),
        notify_activation=True,
    )

    if silent:
        return

    try:
        setup_result = mgr.setup_skill(skill_name)
    except RuntimeError as exc:
        message = str(exc)
        if "setup not supported" in message.lower():
            typer.secho(_("cli.skill.install.setup_not_supported"), fg=typer.colors.YELLOW)
            return
        typer.secho(_("cli.skill.install.setup_failed", error=message), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except Exception as exc:
        typer.secho(_("cli.skill.install.setup_failed", error=str(exc)), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if isinstance(setup_result, dict):
        ok = setup_result.get("ok")
        if ok is False:
            detail = setup_result.get("error") or setup_result.get("message") or ""
            typer.secho(
                _("cli.skill.install.setup_report_failed", detail=str(detail)),
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        detail = setup_result.get("message") or setup_result.get("detail")
        if detail:
            typer.echo(_("cli.skill.install.setup_success_with_detail", detail=str(detail)))
            return

    typer.echo(_("cli.skill.install.setup_success"))


@_run_safe
@app.command("test", help=_("cli.skill.test.help"))
def cmd_test(
    name: str,
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    mgr = _mgr()
    try:
        results = mgr.run_skill_tests(name)
    except Exception as exc:
        typer.secho(f"test failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if json_output:
        typer.echo(json.dumps({k: asdict(v) for k, v in results.items()}, ensure_ascii=False, indent=2))
        if any(res.status != "passed" for res in results.values()):
            raise typer.Exit(1)
        return

    if not results:
        typer.echo("no tests discovered")
        return

    failed = False
    for test_name, result in results.items():
        detail = f" ({result.detail})" if result.detail else ""
        typer.echo(f"{test_name}: {result.status}{detail}")
        if result.status != "passed":
            failed = True

    if failed:
        raise typer.Exit(1)

    typer.secho("tests passed", fg=typer.colors.GREEN)


@app.command("run-handler")
def run_handler(
    skill: str = typer.Argument(..., help=_("cli.skill.run.name_help")),
    topic: str = typer.Option("nlp.intent.weather.get", "--topic", "-t", help=_("cli.skill.run.topic_help")),
    payload: str = typer.Option("{}", "--payload", "-p", help=_("cli.skill.run.payload_help")),
):
    """Execute a skill handler locally using the configured workspace."""

    try:
        payload_obj = json.loads(payload) if payload else {}
        if not isinstance(payload_obj, dict):
            raise ValueError(_("cli.skill.run.payload_type_error"))
    except Exception as exc:
        raise typer.BadParameter(_("cli.skill.run.payload_invalid", error=str(exc)))

    try:
        result = run_skill_handler_sync(skill, topic, payload_obj)
    except SkillRuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(_("cli.skill.run.success", result=repr(result)))


@app.command("run", help=_("cli.skill.run.help"))
def run_tool(
    name: str,
    tool: Optional[str] = typer.Argument(None, help=_("cli.skill.run.tool_help")),
    payload: str = typer.Option("{}", "--json", help=_("cli.skill.run.payload_cli_help")),
    timeout: Optional[float] = typer.Option(None, "--timeout", help=_("cli.skill.run.timeout_help")),
):
    try:
        payload_obj = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        typer.secho(f"invalid payload: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    mgr = _mgr()
    try:
        result = mgr.run_tool(name, tool, payload_obj, timeout=timeout)
    except Exception as exc:
        typer.secho(f"run failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    typer.echo(json.dumps(result, ensure_ascii=False))


@_run_safe
@app.command("setup", help=_("cli.skill.setup.help"))
def cmd_setup(
    name: str,
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    mgr = _mgr()
    try:
        result = mgr.setup_skill(name)
    except Exception as exc:
        typer.secho(f"setup failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if isinstance(result, dict):
        payload = json.dumps(result, ensure_ascii=False)
        typer.echo(payload)
        if not result.get("ok", True):
            raise typer.Exit(1)
        return

    if json_output:
        typer.echo(json.dumps({"result": result}, ensure_ascii=False))
    elif result is None:
        typer.secho("setup completed", fg=typer.colors.GREEN)
    else:
        typer.echo(str(result))


@_run_safe
@app.command("activate")
def activate(name: str, slot: Optional[str] = typer.Option(None, "--slot"), version: Optional[str] = typer.Option(None, "--version")):
    mgr = _mgr()
    try:
        target = mgr.activate_for_space(
            name,
            version=version,
            slot=slot,
            space="default",
            webspace_id=default_webspace_id(),
        )
    except Exception as exc:
        typer.secho(f"activate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    # Best-effort: уведомить живой hub через HTTP API, чтобы
    # skills.activated отработал в его процессе и web_desktop_skill
    # сразу обновил каталог без перезапуска, не трогая ещё раз runtime.
    try:
        base = _hub_base_url().rstrip("/")
        url = base + "/api/skills/runtime/notify-activated"
        payload = {
            "name": name,
            "space": "default",
            "webspace_id": default_webspace_id(),
        }
        headers = _hub_headers(base_url=base)
        # Таймаут маленький и любые ошибки игнорируем, чтобы CLI
        # оставался работоспособен, даже когда API ещё не поднят.
        try:
            requests.post(url, json=payload, headers=headers, timeout=2.0)
        except Exception:
            pass
    except Exception:
        pass

    _refresh_runtime_side_effects(
        name,
        webspace_id=default_webspace_id(),
        notify_activation=True,
    )

    typer.secho(f"skill {name} now active on slot {target}", fg=typer.colors.GREEN)


@_run_safe
@app.command("rollback")
def rollback(name: str):
    mgr = _mgr()
    try:
        slot = mgr.rollback_for_space(name, space="default", webspace_id=default_webspace_id())
    except Exception as exc:
        typer.secho(f"rollback failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"rolled back {name} to slot {slot}", fg=typer.colors.YELLOW)


@_run_safe
@app.command("status")
def status(
    name: Optional[str] = typer.Argument(None, help="skill name (omit to report for all installed skills)"),
    space: str = typer.Option("workspace", "--space", help="workspace | dev"),
    remote: str = typer.Option("origin", "--remote", help="git remote name for comparison"),
    ref: Optional[str] = typer.Option(None, "--ref", help="base git ref (default: <remote>/HEAD or @{u})"),
    fetch: bool = typer.Option(False, "--fetch/--no-fetch", help="git fetch before comparing"),
    diff: bool = typer.Option(False, "--diff", help="print git diff vs base ref (requires NAME)"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    mgr = _mgr()
    ctx = get_ctx()
    space = (space or "workspace").strip().lower()
    if space not in {"workspace", "dev"}:
        typer.secho("--space must be 'workspace' or 'dev'", fg=typer.colors.RED)
        raise typer.Exit(2)

    workspace_root = ctx.paths.workspace_dir()
    skills_root = ctx.paths.skills_workspace_dir()
    dev_skills_root = ctx.paths.dev_skills_dir()
    dev_skills_root = dev_skills_root() if callable(dev_skills_root) else dev_skills_root

    if diff and not name:
        typer.secho("--diff requires a specific skill name", fg=typer.colors.RED)
        raise typer.Exit(2)

    # Workspace: compare against main registry repo.
    REGISTRY_URL = os.getenv("ADAOS_WORKSPACE_REGISTRY_REPO", "https://github.com/stipot-com/adaos-registry.git")
    REGISTRY_REMOTE = os.getenv("ADAOS_WORKSPACE_REGISTRY_REMOTE", "registry")
    REGISTRY_BRANCH = os.getenv("ADAOS_WORKSPACE_REGISTRY_BRANCH", "main")

    if space == "workspace":
        # Ensure expected remote exists; allow user override via --remote/--ref.
        using_default_registry_ref = False
        if remote == "origin" and not ref:
            ensure_remote(workspace_root, name=REGISTRY_REMOTE, url=REGISTRY_URL)
            remote = REGISTRY_REMOTE
            ref = f"{REGISTRY_REMOTE}/{REGISTRY_BRANCH}"
            using_default_registry_ref = True
        if fetch:
            err = fetch_remote(workspace_root, remote=remote)
            if err:
                typer.secho(f"git fetch failed: {err}", fg=typer.colors.YELLOW)

        base_ref = (ref or "").strip() or resolve_base_ref(workspace_root, remote=remote)
        if using_default_registry_ref and base_ref and not ref_exists(workspace_root, base_ref):
            base_ref = (
                resolve_base_ref(workspace_root, remote=REGISTRY_REMOTE)
                or resolve_base_ref(workspace_root, remote="origin")
                or base_ref
            )
    else:
        # Dev: compare local dev folder with the Root backend draft state (API).
        base_ref = None

    installed_names: set[str] = set()
    workspace_registry_by_name: dict[str, dict] = {}
    if space == "workspace":
        try:
            rows = SqliteSkillRegistry(ctx.sql).list()
        except Exception:
            rows = []
        for row in rows:
            n = getattr(row, "name", None) or getattr(row, "id", None)
            if not n or not bool(getattr(row, "installed", True)):
                continue
            installed_names.add(str(n))
        installed_names.update(_collect_runtime_skill_names(Path(skills_root)))
        try:
            registry_items = list_workspace_registry_entries(Path(workspace_root), kind="skills", fallback_to_scan=True)
        except Exception:
            registry_items = []
        for item in registry_items:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name") or item.get("id") or "").strip()
            if item_name:
                workspace_registry_by_name[item_name] = item

    if name:
        names = [name]
    else:
        if space == "dev":
            # In dev space, registry may not reflect local dev folders; prefer filesystem.
            root = Path(dev_skills_root)
            names = []
            if root.exists():
                for child in root.iterdir():
                    if child.is_dir():
                        names.append(child.name)
            names = sorted(set(names))
        else:
            names = []
            for n in installed_names:
                names.append(str(n))
            names = sorted(set(names) | set(_collect_workspace_skill_names(ctx, Path(skills_root))) | set(workspace_registry_by_name))

    results: list[dict] = []
    for skill_name in names:
        runtime_state = None
        runtime_error = None
        source_kind = "dev"
        source_path = Path(dev_skills_root) / skill_name
        source_workdir = Path(dev_skills_root)
        source_base_ref = None
        if space == "workspace":
            source_workdir, source_path, source_kind = _resolve_workspace_skill_source(
                ctx,
                skill_name,
                Path(workspace_root),
                Path(skills_root),
            )
            source_base_ref = base_ref if source_kind == "workspace" else None
            if skill_name in installed_names:
                try:
                    runtime_state = mgr.runtime_status(skill_name)
                except Exception as exc:
                    message = str(exc or "").strip()
                    if message.lower() == "no versions installed":
                        runtime_state = _normalize_runtime_missing_state(skill_name)
                    else:
                        runtime_error = message
            else:
                runtime_state = {
                    "name": skill_name,
                    "installed": False,
                    "ready": False,
                    "state": "draft",
                }

        if space == "workspace":
            registry_meta = workspace_registry_by_name.get(skill_name)
            workspace_version, runtime_version, version_drift = _resolve_workspace_skill_versions(
                runtime_state=runtime_state,
                registry_meta=registry_meta,
                source_path=source_path,
            )
            display_version = _resolve_skill_display_version(
                space=space,
                runtime_state=runtime_state,
                workspace_version=workspace_version,
                runtime_version=runtime_version,
            )
            runtime_flags = _runtime_version_flags(workspace_version, runtime_version, version_drift)
            path_status = compute_path_status(
                workdir=source_workdir,
                path=source_path,
                base_ref=source_base_ref,
            )
            entry = {
                "name": skill_name,
                "space": space,
                "runtime": runtime_state,
                "runtime_error": runtime_error,
                "source": {
                    "kind": source_kind,
                    "path": str(source_path),
                },
                "workspace_registry": registry_meta,
                "workspace_version": workspace_version,
                "runtime_version": runtime_version,
                "display_version": display_version,
                "version_drift": version_drift,
                "runtime_flags": runtime_flags,
                "git": {
                    "path": path_status.path,
                    "exists": path_status.exists,
                    "dirty": path_status.dirty,
                    "flags": _git_path_flags(path_status),
                    "base_ref": path_status.base_ref,
                    "changed_vs_base": path_status.changed_vs_base,
                    "ahead_count": path_status.ahead_count,
                    "behind_count": path_status.behind_count,
                    "local_last_commit": (
                        {
                            "sha": path_status.local_last_commit.sha,
                            "timestamp": path_status.local_last_commit.timestamp,
                            "iso": path_status.local_last_commit.iso,
                            "subject": path_status.local_last_commit.subject,
                        }
                        if path_status.local_last_commit
                        else None
                    ),
                    "base_last_commit": (
                        {
                            "sha": path_status.base_last_commit.sha,
                            "timestamp": path_status.base_last_commit.timestamp,
                            "iso": path_status.base_last_commit.iso,
                            "subject": path_status.base_last_commit.subject,
                        }
                        if path_status.base_last_commit
                        else None
                    ),
                    "error": path_status.error,
                },
            }
        else:
            cfg = load_config()
            base_url = getattr(getattr(cfg, "root_settings", None), "base_url", None) or "https://api.inimatic.com"
            node_id = getattr(getattr(cfg, "node_settings", None), "id", None) or getattr(cfg, "node_id", None) or "hub"
            ca_path = cfg.ca_cert_path()
            cert_path = cfg.hub_cert_path()
            key_path = cfg.hub_key_path()
            verify: str | bool = str(ca_path) if ca_path.exists() else True
            cert = (str(cert_path), str(key_path)) if cert_path.exists() and key_path.exists() else None

            client = RootHttpClient(base_url=base_url)
            local_dir = Path(dev_skills_root) / skill_name
            local_sha256 = None
            try:
                import hashlib

                local_bytes = create_zip_bytes(local_dir)
                local_sha256 = hashlib.sha256(local_bytes).hexdigest()
            except Exception:
                local_sha256 = None

            remote_meta = None
            remote_error = None
            try:
                remote_meta = client.get_skill_draft_info(name=skill_name, node_id=str(node_id), verify=verify, cert=cert)
            except Exception as exc:
                remote_error = str(exc)

            remote_sha256 = None
            if isinstance(remote_meta, dict):
                remote_sha256 = remote_meta.get("sha256")

            changed_vs_base = None
            if local_sha256 and remote_sha256:
                changed_vs_base = str(local_sha256) != str(remote_sha256)

            diff_text = None
            if diff and name == skill_name:
                try:
                    arch = client.get_skill_draft_archive(name=skill_name, node_id=str(node_id), verify=verify, cert=cert)
                    b64 = str(arch.get("archive_b64") or "")
                    if b64:
                        import tempfile

                        with tempfile.TemporaryDirectory() as tmp:
                            remote_dir = Path(tmp) / "remote"
                            remote_dir.mkdir(parents=True, exist_ok=True)
                            unzip_b64_to_dir(archive_b64=b64, dest=remote_dir)
                            _changed, diff_out = render_noindex_diff(left=remote_dir, right=local_dir)
                            diff_text = diff_out
                except Exception:
                    diff_text = None

            entry = {
                "name": skill_name,
                "space": space,
                "dev_compare": {
                    "node_id": str(node_id),
                    "base_url": base_url,
                    "local_path": local_dir.as_posix(),
                    "local_sha256": local_sha256,
                    "remote": remote_meta,
                    "remote_error": remote_error,
                    "changed_vs_base": changed_vs_base,
                    "diff": diff_text,
                },
            }
        results.append(entry)

    if json_output:
        payload = {"skills": results}
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not results:
        typer.echo("No installed skills.")
        return

    if name:
        entry = results[0] if results else {}
        st = entry.get("runtime") or {}
        g = entry.get("git") or {}
        source = entry.get("source") or {}
        reg = entry.get("workspace_registry") or {}
        display_version = str(entry.get("display_version") or "").strip()
        runtime_version = str(entry.get("runtime_version") or "").strip()
        runtime_flags = [str(flag) for flag in (entry.get("runtime_flags") or []) if str(flag).strip()]
        typer.echo(f"skill: {entry.get('name')}")
        typer.echo(f"space: {entry.get('space')}")
        if space == "workspace" and display_version and display_version != "n/a":
            typer.echo(f"version: {display_version}")
        if entry.get("runtime_error"):
            typer.secho(f"runtime: error: {entry.get('runtime_error')}", fg=typer.colors.YELLOW)
        elif space == "workspace":
            state = str(st.get("state") or "").strip()
            is_registry_published = bool(reg)
            if st.get("installed") is False or state in {"draft", "runtime-missing"}:
                if state == "runtime-missing":
                    message = "runtime: not installed in runtime yet"
                elif is_registry_published:
                    published_version = reg.get("version") or "unknown"
                    message = f"runtime: not installed in runtime yet (workspace registry v{published_version})"
                else:
                    message = "runtime: draft (not installed in runtime yet)"
                typer.echo(message)
            else:
                typer.echo(f"active slot: {st.get('active_slot')}")
                if entry.get("version_drift") and runtime_version:
                    typer.echo(f"runtime version: {runtime_version}")
                if runtime_flags:
                    typer.echo("runtime status: " + ", ".join(runtime_flags))
            if st.get("installed") is False or state in {"draft", "runtime-missing"}:
                typer.echo("resolved manifest: (not installed)")
            elif st.get("ready", True):
                typer.echo(f"resolved manifest: {st.get('resolved_manifest')}")
            else:
                typer.echo("resolved manifest: (not activated)")
                pending_slot = st.get("pending_slot")
                hint_slot = pending_slot or st.get("active_slot")
                activation_hint = f" --slot {pending_slot}" if pending_slot else ""
                typer.secho(
                    f"slot {hint_slot} is prepared but inactive. run 'adaos skill activate {name}{activation_hint}'",
                    fg=typer.colors.YELLOW,
                )
            tests = st.get("tests") or {}
            if tests:
                typer.echo("tests: " + ", ".join(f"{k}={v}" for k, v in tests.items()))
            default_tool = st.get("default_tool")
            if default_tool:
                typer.echo(f"default tool: {default_tool}")
            if source.get("kind"):
                typer.echo(f"source: {source.get('kind')}")
            if source.get("path"):
                typer.echo(f"source path: {source.get('path')}")

        if space == "workspace":
            typer.echo(f"git path: {g.get('path')}")
            typer.echo(f"git base: {g.get('base_ref') or '(none)'}")
            if g.get("error"):
                typer.secho(f"git: {g.get('error')}", fg=typer.colors.YELLOW)
            else:
                flags = _git_path_flags(g)
                typer.echo("git status: " + (", ".join(flags) if flags else "clean"))
                ahead = _positive_int(g.get("ahead_count"))
                behind = _positive_int(g.get("behind_count"))
                if ahead or behind:
                    typer.echo(f"git divergence: ahead={ahead} behind={behind}")
                if g.get("local_last_commit"):
                    lc = g["local_last_commit"]
                    typer.echo(f"last local: {lc.get('sha')} {lc.get('iso') or lc.get('timestamp')} {lc.get('subject')}")
                if g.get("base_last_commit"):
                    bc = g["base_last_commit"]
                    typer.echo(f"last base:  {bc.get('sha')} {bc.get('iso') or bc.get('timestamp')} {bc.get('subject')}")

            if diff:
                if not base_ref:
                    typer.secho("cannot diff: base ref is not available", fg=typer.colors.YELLOW)
                else:
                    try:
                        typer.echo(render_diff(workspace_root, base_ref=base_ref, path=str(g.get("path") or "")))
                    except Exception as exc:
                        typer.secho(f"diff failed: {exc}", fg=typer.colors.RED)
                        raise typer.Exit(1) from exc
        else:
            dc = entry.get("dev_compare") or {}
            typer.echo(f"root base: {dc.get('base_url')}")
            typer.echo(f"node_id: {dc.get('node_id')}")
            typer.echo(f"local path: {dc.get('local_path')}")
            if dc.get("changed_vs_base") is True:
                typer.secho("status: diff", fg=typer.colors.YELLOW)
            elif dc.get("changed_vs_base") is False:
                typer.echo("status: clean")
            else:
                typer.secho("status: unknown", fg=typer.colors.YELLOW)
            if diff and dc.get("diff"):
                typer.echo(dc.get("diff") or "")
        return

    # Summary for all skills
    for entry in results:
        st = entry.get("runtime") or {}
        g = entry.get("git") or {}
        reg = entry.get("workspace_registry") or {}
        flags: list[str] = []
        if entry.get("runtime_error"):
            flags.append("runtime-error")
        elif space == "workspace":
            state = str(st.get("state") or "").strip()
            if st.get("installed") is False and not reg and state == "draft":
                flags.append("draft")
            elif state == "runtime-missing":
                flags.append("runtime-missing")
            flags.extend(str(flag) for flag in (entry.get("runtime_flags") or []) if str(flag).strip())
        if space == "workspace":
            flags.extend(_git_path_flags(g))
        else:
            dc = entry.get("dev_compare") or {}
            if dc.get("changed_vs_base"):
                flags.append("diff")
        version = entry.get("display_version") or (
            "n/a" if space == "dev" or st.get("installed") is False or str(st.get("state") or "").strip() in {"draft", "runtime-missing"} else "unknown"
        )
        slot = st.get("active_slot") or ("n/a" if space == "dev" else "n/a")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        typer.echo(f"{entry.get('name')}: v{version} slot={slot}{suffix}")


@_run_safe
@app.command("gc")
def gc(name: Optional[str] = typer.Option(None, "--name", help="skill to clean")):
    mgr = _mgr()
    cleaned = mgr.gc_runtime(name)
    for skill, versions in cleaned.items():
        removed = ", ".join(versions) if versions else "nothing"
        typer.echo(f"gc {skill}: removed {removed}")


@_run_safe
@app.command("doctor")
def doctor(name: str):
    mgr = _mgr()
    try:
        info = mgr.doctor_runtime(name)
    except Exception as exc:
        typer.secho(f"doctor failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(info, ensure_ascii=False, indent=2))


@_run_safe
@app.command("migrate")
def migrate(
    name: Optional[str] = typer.Argument(None, help="skill to migrate"),
    dry_run: bool = typer.Option(False, "--dry-run", help="report without applying changes"),
    force: bool = typer.Option(False, "--force", help=_("cli.skill.migrate.option.force")),
    local: bool = typer.Option(False, "--local", help="Force local execution (bypass hub API)."),
):
    if not local and _hub_api_ready(timeout_s=3.0):
        if dry_run:
            typer.secho("dry-run is not supported in hub api mode; ignoring", fg=typer.colors.YELLOW)
        if name:
            result = _hub_post(
                "/api/skills/update",
                body={
                    "name": name,
                    "dry_run": False,
                    "webspace_id": default_webspace_id(),
                    **({"force": True} if force else {}),
                },
                timeout_s=120,
            )
            typer.echo(
                f"{name}: {'updated' if result.get('updated') else 'up-to-date'}"
                + (f" (version {result.get('version')})" if result.get("version") else "")
            )
            return
        _hub_post("/api/skills/sync", body={"force": True} if force else None)
        listing = _hub_get("/api/skills/list")
        items = listing.get("items") if isinstance(listing, dict) else []
        if not isinstance(items, list):
            items = []
        updated_any = False
        failed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            skill_name = str(item.get("name") or item.get("id") or "").strip()
            if not skill_name:
                continue
            try:
                result = _hub_post(
                    "/api/skills/update",
                    body={
                        "name": skill_name,
                        "dry_run": False,
                        "webspace_id": default_webspace_id(),
                        "defer_webspace_rebuild": True,
                        **({"force": True} if force else {}),
                    },
                    timeout_s=120,
                )
            except Exception as exc:
                failed = True
                typer.secho(f"{skill_name}: {exc}", fg=typer.colors.RED)
                continue
            updated_any = True
            typer.echo(
                f"{skill_name}: {'updated' if result.get('updated') else 'up-to-date'}"
                + (f" (version {result.get('version')})" if result.get("version") else "")
            )
        if updated_any:
            try:
                _rebuild_hub_webspace(webspace_id=default_webspace_id())
            except Exception as exc:
                failed = True
                typer.secho(f"webspace rebuild failed: {exc}", fg=typer.colors.RED)
        if failed:
            raise typer.Exit(1)
        return
    ctx = get_ctx()
    service = SkillUpdateService(ctx)
    mgr = _mgr()
    names: list[str]
    if name:
        names = [name]
    else:
        if not dry_run:
            try:
                mgr.sync(force=force)
            except Exception as exc:
                typer.secho(f"failed to sync workspace skills: {exc}", fg=typer.colors.RED)
                raise typer.Exit(1) from exc
        try:
            names = _list_migratable_workspace_skills(ctx=ctx, mgr=mgr)
        except Exception as exc:
            typer.secho(f"failed to detect changed skills: {exc}", fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        if not names:
            typer.echo("no changed skills detected")
            return

    failed = False
    updated_any = False
    for skill_name in names:
        try:
            kwargs = {"dry_run": dry_run}
            if force:
                kwargs["force"] = True
            result = service.request_update(skill_name, **kwargs)
        except FileNotFoundError as exc:
            failed = True
            typer.secho(f"{skill_name}: {exc}", fg=typer.colors.RED)
            continue
        typer.echo(
            f"{skill_name}: {'updated' if result.updated else 'up-to-date'}"
            + (f" (version {result.version})" if result.version else "")
        )
        if not dry_run:
            updated_any = True
            try:
                refresh_skill_runtime(
                    mgr,
                    skill_name,
                    webspace_id=default_webspace_id(),
                    source_version=result.version,
                    migrate_runtime=True,
                    ensure_installed=True,
                )
            except Exception as exc:
                failed = True
                typer.secho(f"{skill_name}: runtime refresh failed: {exc}", fg=typer.colors.RED)
                continue
            _refresh_runtime_side_effects(
                skill_name,
                webspace_id=default_webspace_id(),
                notify_activation=True,
                emit_updated=True,
                defer_hub_rebuild=True,
                rebuild_local=False,
            )
    if updated_any:
        _rebuild_local_webspace(webspace_id=default_webspace_id())
    if failed:
        raise typer.Exit(1)
