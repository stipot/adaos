"""Typer commands for managing and executing scenarios."""

from __future__ import annotations

import json
import os
import traceback
import asyncio
from pathlib import Path
from typing import Optional

import typer

from adaos.adapters.db import SqliteScenarioRegistry
from adaos.apps.cli.i18n import _
from adaos.apps.cli.git_status import (
    compute_path_status,
    fetch_remote,
    unzip_b64_to_dir,
    render_noindex_diff,
    render_diff,
    resolve_base_ref,
)
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from adaos.services.root.client import RootHttpClient
from adaos.services.root.service import create_zip_bytes
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources
from adaos.services.scenario.scaffold import create as scaffold_create
from adaos.services.workspace_registry import build_registry_entry, list_workspace_registry_entries
from adaos.services.yjs.webspace import default_webspace_id
from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context, load_scenario

app = typer.Typer(help=_("cli.help_scenario"))


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


def _artifact_name_from_meta(meta: object) -> str:
    path = str(getattr(meta, "path", "") or "").strip()
    if path:
        name = Path(path).name.strip()
        if name:
            return name
    return str(getattr(meta, "name", None) or getattr(getattr(meta, "id", None), "value", None) or "").strip()


def _run_safe(func):
    """Wrap Typer callbacks to surface tracebacks when ADAOS_CLI_DEBUG=1."""

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _mgr() -> ScenarioManager:
    ctx = get_ctx()
    repo = ctx.scenarios_repo
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _resolve_scenario_path(target: str) -> Path:
    candidate = Path(target).expanduser()
    if candidate.exists():
        return candidate.resolve()
    ctx = get_ctx()
    attr = getattr(ctx.paths, "scenarios_workspace_dir", None)
    if attr is not None:
        root = attr() if callable(attr) else attr
    else:
        base = getattr(ctx.paths, "scenarios_dir")
        root = base() if callable(base) else base
    candidate = (Path(root) / target).resolve()
    if candidate.exists():
        return candidate
    raise typer.BadParameter(_("cli.scenario.push.not_found", name=target))


@_run_safe
@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    show_fs: bool = typer.Option(False, "--fs", help=_("cli.option.fs")),
):
    """List installed scenarios from the registry."""

    mgr = _mgr()
    rows = mgr.list_installed()
    present = list(mgr.list_present() or [])
    fallback_rows = [
        {
            "name": _artifact_name_from_meta(meta),
            "version": str(getattr(meta, "version", None) or "unknown"),
        }
        for meta in present
        if _artifact_name_from_meta(meta)
    ]

    if json_output:
        scenarios = [
            {
                "name": r.name,
                "version": getattr(r, "active_version", None) or "unknown",
            }
            for r in rows
            if bool(getattr(r, "installed", True))
        ]
        if not scenarios:
            scenarios = fallback_rows
        payload = {
            "scenarios": scenarios
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    if not rows:
        if fallback_rows:
            for item in fallback_rows:
                typer.echo(_("cli.scenario.list.item", name=item["name"], version=item["version"]))
        else:
            typer.echo(_("cli.scenario.list.empty"))
    else:
        for r in rows:
            if not bool(getattr(r, "installed", True)):
                continue
            version = getattr(r, "active_version", None) or "unknown"
            typer.echo(_("cli.scenario.list.item", name=r.name, version=version))

    if show_fs:
        present = {name for m in mgr.list_present() for name in [_artifact_name_from_meta(m)] if name}
        desired = {r.name for r in rows if bool(getattr(r, "installed", True))}
        missing = desired - present
        extra = present - desired
        if missing:
            typer.echo(_("cli.scenario.fs_missing", items=", ".join(sorted(missing))))
        if extra:
            typer.echo(_("cli.scenario.fs_extra", items=", ".join(sorted(extra))))


@_run_safe
@app.command("status")
def status(
    name: Optional[str] = typer.Argument(None, help="scenario name (omit to report for all installed scenarios)"),
    space: str = typer.Option("workspace", "--space", help="workspace | dev"),
    remote: str = typer.Option("origin", "--remote", help="git remote name for comparison"),
    ref: Optional[str] = typer.Option(None, "--ref", help="base git ref (default: <remote>/HEAD or @{u})"),
    fetch: bool = typer.Option(False, "--fetch/--no-fetch", help="git fetch before comparing"),
    diff: bool = typer.Option(False, "--diff", help="print git diff vs base ref (requires NAME)"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    ctx = get_ctx()
    space = (space or "workspace").strip().lower()
    if space not in {"workspace", "dev"}:
        typer.secho("--space must be 'workspace' or 'dev'", fg=typer.colors.RED)
        raise typer.Exit(2)

    workspace_root = ctx.paths.workspace_dir()
    scenarios_root = ctx.paths.scenarios_workspace_dir()
    dev_scenarios_root = ctx.paths.dev_scenarios_dir()
    dev_scenarios_root = dev_scenarios_root() if callable(dev_scenarios_root) else dev_scenarios_root

    if diff and not name:
        typer.secho("--diff requires a specific scenario name", fg=typer.colors.RED)
        raise typer.Exit(2)

    REGISTRY_URL = os.getenv("ADAOS_WORKSPACE_REGISTRY_REPO", "https://github.com/inimatic/adaos-registry.git")
    REGISTRY_REMOTE = os.getenv("ADAOS_WORKSPACE_REGISTRY_REMOTE", "registry")
    REGISTRY_BRANCH = os.getenv("ADAOS_WORKSPACE_REGISTRY_BRANCH", "main")

    if space == "workspace":
        if remote == "origin" and not ref:
            # Best-effort: align default remote to registry/main.
            from adaos.apps.cli.git_status import ensure_remote

            ensure_remote(workspace_root, name=REGISTRY_REMOTE, url=REGISTRY_URL)
            remote = REGISTRY_REMOTE
            ref = f"{REGISTRY_REMOTE}/{REGISTRY_BRANCH}"
        if fetch:
            err = fetch_remote(workspace_root, remote=remote)
            if err:
                typer.secho(f"git fetch failed: {err}", fg=typer.colors.YELLOW)
        base_ref = (ref or "").strip() or resolve_base_ref(workspace_root, remote=remote)
    else:
        base_ref = None

    workspace_registry_by_name = {}
    if space == "workspace":
        try:
            registry_items = list_workspace_registry_entries(Path(workspace_root), kind="scenarios", fallback_to_scan=True)
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
            root = Path(dev_scenarios_root)
            names = []
            if root.exists():
                for child in root.iterdir():
                    if child.is_dir():
                        names.append(child.name)
            names = sorted(set(names))
        else:
            try:
                rows = SqliteScenarioRegistry(ctx.sql).list()
            except Exception:
                rows = []
            names = []
            for row in rows:
                n = getattr(row, "name", None) or getattr(row, "id", None)
                if not n or not bool(getattr(row, "installed", True)):
                    continue
                names.append(str(n))
            names = sorted(set(names) | set(_workspace_child_names(Path(scenarios_root))) | set(workspace_registry_by_name))

    rows_by_name = {}
    try:
        for r in SqliteScenarioRegistry(ctx.sql).list():
            rows_by_name[str(getattr(r, "name", None) or getattr(r, "id", ""))] = r
    except Exception:
        rows_by_name = {}

    results: list[dict] = []
    for scenario_name in names:
        row = rows_by_name.get(scenario_name)
        if space == "workspace":
            local_dir = Path(scenarios_root) / scenario_name
            registry_meta = workspace_registry_by_name.get(scenario_name)
            version = (
                _read_local_artifact_version("scenarios", local_dir)
                or _clean_version_text((registry_meta or {}).get("version") if isinstance(registry_meta, dict) else None)
                or _clean_version_text(getattr(row, "active_version", None) if row is not None else None)
                or "unknown"
            )
            path_status = compute_path_status(
                workdir=workspace_root,
                path=local_dir,
                base_ref=base_ref,
            )
            results.append(
                {
                    "name": scenario_name,
                    "space": space,
                    "version": version,
                    "workspace_registry": registry_meta,
                    "git": {
                        "path": path_status.path,
                        "exists": path_status.exists,
                        "dirty": path_status.dirty,
                        "base_ref": path_status.base_ref,
                        "changed_vs_base": path_status.changed_vs_base,
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
            )
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
            local_dir = Path(dev_scenarios_root) / scenario_name
            version = (
                _read_local_artifact_version("scenarios", local_dir)
                or _clean_version_text(getattr(row, "active_version", None) if row is not None else None)
                or "unknown"
            )
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
                remote_meta = client.get_scenario_draft_info(name=scenario_name, node_id=str(node_id), verify=verify, cert=cert)
            except Exception as exc:
                remote_error = str(exc)

            remote_sha256 = None
            if isinstance(remote_meta, dict):
                remote_sha256 = remote_meta.get("sha256")

            changed_vs_base = None
            if local_sha256 and remote_sha256:
                changed_vs_base = str(local_sha256) != str(remote_sha256)

            diff_text = None
            if diff and name == scenario_name:
                try:
                    arch = client.get_scenario_draft_archive(name=scenario_name, node_id=str(node_id), verify=verify, cert=cert)
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

            results.append(
                {
                    "name": scenario_name,
                    "space": space,
                    "version": version,
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
            )

    if json_output:
        typer.echo(json.dumps({"scenarios": results}, ensure_ascii=False, indent=2))
        return

    if not results:
        typer.echo("No installed scenarios.")
        return

    if name:
        entry = results[0] if results else {}
        g = entry.get("git") or {}
        typer.echo(f"scenario: {entry.get('name')}")
        typer.echo(f"space: {entry.get('space')}")
        typer.echo(f"version: {entry.get('version')}")
        if space == "workspace":
            typer.echo(f"git path: {g.get('path')}")
            typer.echo(f"git base: {g.get('base_ref') or '(none)'}")
            if g.get("error"):
                typer.secho(f"git: {g.get('error')}", fg=typer.colors.YELLOW)
            else:
                flags: list[str] = []
                if g.get("dirty"):
                    flags.append("dirty")
                if g.get("changed_vs_base"):
                    flags.append("diff")
                typer.echo("git status: " + (", ".join(flags) if flags else "clean"))
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

    for entry in results:
        g = entry.get("git") or {}
        flags: list[str] = []
        if space == "workspace":
            if g.get("dirty"):
                flags.append("dirty")
            if g.get("changed_vs_base"):
                flags.append("diff")
        else:
            dc = entry.get("dev_compare") or {}
            if dc.get("changed_vs_base"):
                flags.append("diff")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        typer.echo(f"{entry.get('name')}: v{entry.get('version')}{suffix}")


@_run_safe
@app.command("sync")
def sync_cmd():
    """Apply sparse checkout for scenarios and pull the repository."""

    mgr = _mgr()
    mgr.sync()
    typer.echo(_("cli.scenario.sync.done"))


@_run_safe
@app.command("install")
def install_cmd(
    name: str = typer.Argument(..., help=_("cli.scenario.install.name_help")),
    pin: Optional[str] = typer.Option(None, "--pin", help=_("cli.scenario.install.pin_help")),
):
    """Install a scenario into the workspace monorepo."""

    mgr = _mgr()
    # Stage A2: use extended install that also applies dependencies.
    try:
        meta = mgr.install_with_deps(name, pin=pin, webspace_id=default_webspace_id())
    except Exception as exc:
        typer.secho(f"install failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    try:
        asyncio.run(
            rebuild_webspace_from_sources(
                default_webspace_id(),
                action="scenario_install_sync",
                scenario_id=meta.id.value,
                source_of_truth="scenario_projection",
            )
        )
    except Exception:
        pass
    typer.echo(_("cli.scenario.install.done", name=meta.id.value, version=meta.version, path=meta.path))


@_run_safe
@app.command("create")
def create_cmd(
    scenario_id: str = typer.Argument(..., help=_("cli.scenario.create.name_help")),
    template: str = typer.Option("scenario_default", "--template", "-t", help=_("cli.scenario.create.template_help")),
):
    """Create a new scenario scaffold from a template. Deprecated. Use adaos dev scenario create"""
    typer.secho("Deprecated. Use adaos dev scenario create.", fg=typer.colors.RED)
    raise typer.Exit(1)


@_run_safe
@app.command("uninstall")
def uninstall_cmd(
    name: str = typer.Argument(..., help=_("cli.scenario.uninstall.name_help")),
    safe: bool = typer.Option(False, "--safe", help=_("cli.scenario.uninstall.option.safe")),
):
    """Uninstall a scenario by removing it from registry and sparse checkout."""

    mgr = _mgr()
    mgr.uninstall(name, safe=safe)
    try:
        asyncio.run(
            rebuild_webspace_from_sources(
                default_webspace_id(),
                action="scenario_uninstall_sync",
                source_of_truth="scenario_projection",
            )
        )
    except Exception:
        pass
    typer.echo(_("cli.scenario.uninstall.done", name=name))


@_run_safe
@app.command("push", context_settings={"allow_extra_args": True, "ignore_unknown_options": False})
def push_command(
    ctx: typer.Context,
    scenario_name: str = typer.Argument(..., help=_("cli.scenario.push.name_help")),
    message: Optional[str] = typer.Option(None, "--message", "-m", help=_("cli.commit_message.help")),
    signoff: bool = typer.Option(False, "--signoff", help=_("cli.option.signoff")),
):
    """Commit changes inside a scenario directory and push to remote."""

    extra = [str(item) for item in getattr(ctx, "args", []) or []]
    if extra:
        if any(part.startswith("-") for part in extra):
            raise typer.BadParameter(f"unexpected extra arguments: {' '.join(extra)}")
        message = " ".join([part for part in ([message] if message else []) + extra if str(part).strip()]).strip() or None

    if message is None:
        typer.secho(
            "Root publishing via 'adaos scenario push' has moved to 'adaos dev scenario push'.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Use --message/-m to push commits or run 'adaos dev scenario push <name>'.")
        raise typer.Exit(1)

    mgr = _mgr()
    _resolve_scenario_path(scenario_name)
    result = mgr.push(scenario_name, message, signoff=signoff)
    if result in {"nothing-to-push", "nothing-to-commit"}:
        typer.echo(_("cli.scenario.push.nothing"))
    else:
        typer.echo(_("cli.scenario.push.done", name=scenario_name, revision=result))


@_run_safe
@app.command("run")
def run_cmd(
    scenario_id: str = typer.Argument(..., help=_("cli.scenario.run.name_help")),
    path: Optional[str] = typer.Option(None, "--path", help=_("cli.scenario.run.path_help")),
) -> None:
    ctx = get_ctx()
    scenario_path = (path if path else ctx.paths.scenarios_workspace_dir()) / scenario_id
    runtime = ScenarioRuntime()
    result = runtime.run_from_file(str(scenario_path))
    meta = result.get("meta") or {}
    log_file = meta.get("log_file")
    typer.secho(_("cli.scenario.run.success", scenario_id=scenario_id), fg=typer.colors.GREEN)
    if log_file:
        typer.echo(_("cli.scenario.run.log", path=log_file))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@_run_safe
@app.command("validate")
def validate_cmd(
    scenario_id: str = typer.Argument(..., help=_("cli.scenario.validate.name_help")),
    path: Optional[Path] = typer.Option(None, "--path", help=_("cli.scenario.validate.path_help")),
    json_output: bool = typer.Option(False, "--json", help="machine readable output"),
) -> None:
    """
    Validate scenario from workspace (default), dev space, or explicit --path.
    """
    ctx = get_ctx()
    if path:
        scenario_path = Path(path).expanduser().resolve()
        if scenario_path.is_dir():
            scenario_path = scenario_path
        else:
            # если указали путь до файла – поддержим и это
            scenario_path = scenario_path.parent
    else:
        scenario_path = ctx.paths.scenarios_workspace_dir()
    scenario_path = scenario_path / scenario_id
    model = load_scenario(scenario_path)
    runtime = ScenarioRuntime()
    errors = runtime.validate(model)

    if json_output:
        payload = {"ok": not bool(errors), "errors": errors, "scenario_id": model.id}
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if not errors else 1)

    if errors:
        typer.secho(_("cli.scenario.validate.errors"), fg=typer.colors.RED)
        for err in errors:
            typer.echo(_("cli.scenario.validate.error_item", error=str(err)))
        raise typer.Exit(code=1)
    typer.secho(_("cli.scenario.validate.success", scenario_id=model.id), fg=typer.colors.GREEN)


def _collect_scenario_tests(scenario_id: Optional[str]) -> list[Path]:
    ctx = get_ctx()
    root = ctx.paths.scenarios_workspace_dir()
    tests: list[Path] = []
    if not root.exists():
        return tests
    if scenario_id:
        candidates = [root / scenario_id / "tests"]
    else:
        candidates = [p / "tests" for p in root.iterdir() if p.is_dir()]
    for tests_dir in candidates:
        if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
            tests.append(tests_dir)
    return tests


@_run_safe
@app.command("test")
def test_cmd(
    scenario_id: Optional[str] = typer.Argument(None, help=_("cli.scenario.test.name_help")),
    extra: Optional[str] = typer.Option(None, "--pytest-args", help=_("cli.scenario.test.extra_help")),
) -> None:
    tests = _collect_scenario_tests(scenario_id)
    if not tests:
        typer.secho(_("cli.scenario.test.none"), fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    args = ["pytest", "-q", *[str(p) for p in tests]]
    if extra:
        args.extend(extra.split())

    command = " ".join(args)
    typer.echo(_("cli.scenario.test.running", command=command))
    result = subprocess.run(args, text=True)
    raise typer.Exit(code=result.returncode)


__all__ = ["app"]
