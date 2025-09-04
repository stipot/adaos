from __future__ import annotations
import json, os, shutil
from pathlib import Path
from git import Repo
from typing import Any, Dict, List, Optional
from adaos.sdk.context import _agent
from adaos.sdk.utils import git_utils as _git
from adaos.agent.core.scenario_engine.dsl import Prototype
from adaos.agent.db.sqlite import (
    add_or_update_entity,
    add_or_update_entity,
    list_entities,
    set_installed_flag,
)

TEMPL_ROOT = Path(__file__).resolve().parents[1] / "scenario_templates"  # src/adaos/scenario_templates

META_FILE = ".meta.json"
IGNORES = shutil.ignore_patterns("impl", "bindings", ".git", "__pycache__")


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


# -------------- sparse-checkout (аналог _sync_sparse_checkout) --------------


def read_meta(sid: str) -> Dict[str, Any]:
    p = _agent.scenarios_dir / sid / META_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_meta(sid: str, data: Dict[str, Any]) -> Path:
    p = meta_path(sid)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def scenarios_base() -> Path:
    return _ensure_dir(_agent.scenarios_dir)


def scenario_dir(sid: str) -> Path:
    return _agent.scenarios_dir / sid


def scenario_proto_path(sid: str) -> Path:
    return _agent.scenarios_dir / sid / "scenario.json"


def impl_dir(sid: str, user: str) -> Path:
    return scenario_dir(sid) / "impl" / user


def bindings_dir(sid: str) -> Path:
    return scenario_dir(sid) / "bindings"


def bindings_path(sid: str, user: str) -> Path:
    return bindings_dir(sid) / f"{user}.json"


def meta_path(sid: str) -> Path:
    return scenario_dir(sid) / META_FILE


def list_installed() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not _agent.scenarios_dir.exists():
        return out
    for d in sorted(_agent.scenarios_dir.iterdir()):
        p = d / "scenario.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                meta = read_meta(d.name)
                out.append({"id": data.get("id") or d.name, "name": data.get("name"), "version": data.get("version"), "path": str(p), "meta": meta or None})
            except Exception:
                out.append({"id": d.name, "path": str(p)})
    return out


def read_prototype(sid: str) -> Dict[str, Any]:
    p = scenario_proto_path(sid)
    if not p.exists():
        raise FileNotFoundError(f"scenario not found: {sid}")
    return json.loads(p.read_text(encoding="utf-8"))


def write_prototype(sid: str, data: Dict[str, Any]) -> Path:
    d = scenario_dir(sid)
    _ensure_dir(d)
    p = d / "scenario.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# -------------- создание/установка/обновление --------------


def create_scenario(sid: str, template: str = "template") -> Path:
    """
    Создаёт новый сценарий SID, копируя **все файлы** из шаблона (включая .gitignore):
    src/adaos/scenario_templates/<template>/* → <base>/scenarios/<sid>/*
    Добавляет в sparse, коммитит и (не в тест-режиме) пушит.
    """
    repo = _git._ensure_repo(_agent.scenarios_dir, _agent.monorepo_scens_url)

    src_dir = TEMPL_ROOT / template
    if not src_dir.exists():
        raise FileNotFoundError(f"template not found: {src_dir}")

    dst_dir = scenario_dir(sid)
    if dst_dir.exists():
        raise FileExistsError(f"scenario already exists: {sid}")
    shutil.copytree(src_dir, dst_dir)  # копируем все файлы
    _ensure_dir(dst_dir / "impl")
    _ensure_dir(dst_dir / "bindings")

    # включаем в sparse и коммитим
    try:
        _git.sparse_add(repo, sid)
    except Exception:
        pass
    repo.git.add(sid)
    if repo.is_dirty():
        repo.git.commit("-m", f"Create scenario {sid} from template {template}")
        if os.environ.get("ADAOS_TESTING") != "1":
            try:
                repo.remotes.origin.push()
            except Exception:
                pass

    # версия из scenario.json
    data = json.loads((dst_dir / "scenario.json").read_text(encoding="utf-8"))
    version = data.get("version", "unknown")
    add_or_update_entity("scenario", sid, version, repo.remotes.origin.url, installed=1)
    write_meta(sid, {"source": {"repo": repo.remotes.origin.url, "sid": sid, "ref": None, "subpath": None, "commit": _git.current_commit(repo.working_dir)}})

    return dst_dir / "scenario.json"


def install_from_repo(repo_url: str, sid: Optional[str] = None, ref: Optional[str] = None, subpath: Optional[str] = None) -> Path:
    """
    Поддерживаем прежний эндпойнт: монорепо уже у нас в <base>/scenarios>.
    Просто переключаемся на ref (если задан), включаем sid в sparse и убеждаемся, что он есть.
    """
    repo = _git._ensure_repo(_agent.scenarios_dir, _agent.monorepo_scens_url)
    if ref:
        _git.checkout_ref(str(repo.working_dir), ref)
    else:
        try:
            repo.remotes.origin.fetch(prune=True)
        except Exception:
            pass

    # sid обязателен для монорепо; auto-detect — если в корне ровно одна папка с scenario.json
    if not sid:
        candidates = [p.parent.name for p in Path(repo.working_dir).glob("*/scenario.json")]
        candidates = [c for c in candidates if c not in (".git", "impl", "bindings")]
        if len(candidates) != 1:
            raise RuntimeError("ambiguous repo layout, specify sid")
        sid = candidates[0]

    try:
        _git.sparse_add(repo, sid)
    except Exception:
        pass

    scen_p = scenario_proto_path(sid)
    if not scen_p.exists():
        raise FileNotFoundError(f"scenario.json not found: {scen_p}")

    # версия + регистрация
    data = json.loads(scen_p.read_text(encoding="utf-8"))
    version = data.get("version", "unknown")
    add_or_update_entity("scenario", sid, version, repo.remotes.origin.url, installed=1)

    write_meta(sid, {"source": {"repo": repo.remotes.origin.url, "sid": sid, "ref": ref, "subpath": subpath, "commit": _git.current_commit(repo.working_dir)}})
    return scen_p


def update_from_repo(sid: str, ref: Optional[str] = None) -> Path:
    repo = _git._ensure_repo(_agent.scenarios_dir, _agent.monorepo_scens_url)
    meta = read_meta(sid)
    target_ref = ref or (meta.get("source") or {}).get("ref")
    if target_ref:
        _git.checkout_ref(str(repo.working_dir), target_ref)
    else:
        try:
            repo.remotes.origin.pull(rebase=False)
        except Exception:
            pass

    try:
        _git.sparse_add(repo, sid)
    except Exception:
        pass

    scen_p = scenario_proto_path(sid)
    if not scen_p.exists():
        raise FileNotFoundError(f"scenario.json not found after update: {scen_p}")

    # обновим commit в meta
    meta.setdefault("source", {})["commit"] = _git.current_commit(repo.working_dir)
    write_meta(sid, meta)
    return scen_p


# -------------- аналоги pull/install/uninstall как у skills --------------


def pull_scenario(sid: str) -> str:
    """
    «Установить/обновить» сценарий SID из удалённого монорепо:
    - помечаем installed=1
    - синхронизируем sparse (оставляем только установленные)
    - git pull
    - читаем version из scenario.json
    - валидируем DSL (pydantic)
    """
    repo = _git._ensure_repo(_agent.scenarios_dir, _agent.monorepo_scens_url)
    set_installed_flag("scenario", sid, installed=1)
    _git._sync_sparse_checkout(repo, el_list=list_entities("scenario"), installed=[sid])
    try:
        repo.remotes.origin.pull(rebase=False)
    except Exception:
        pass

    p = scenario_proto_path(sid)
    print("p_log", p, _agent.scenarios_dir, _agent.monorepo_scens_url)
    if not p.exists():
        return f"[red]scenario '{sid}' not found in repo[/red]"
    data = json.loads(p.read_text(encoding="utf-8"))
    version = data.get("version", "unknown")

    # простая валидация DSL
    try:
        Prototype.model_validate(data)
    except Exception as e:
        # откат installed, если невалидно
        set_installed_flag("scenario", sid, installed=0)
        _git._sync_sparse_checkout(repo, el_list=list_entities("scenario"))
        return f"[red]scenario '{sid}' validation failed[/red] {e}"

    add_or_update_entity("scenario", sid, version, repo.remotes.origin.url, installed=1)
    return f"[green]scenario '{sid}' pulled[/green] (version: {version})"


def push_scenario(sid: str, message: Optional[str] = None) -> str:
    """
    Коммитит и пушит изменения в каталоге сценария <base>/scenarios/<sid>.
    Ожидается, что в сценарии есть .gitignore, исключающий impl/ и bindings/.
    """
    repo = _git._ensure_repo(_agent.scenarios_dir, _agent.monorepo_scens_url)

    # гарантируем, что папка сценария есть в sparse
    try:
        _git.sparse_add(repo, sid)
    except Exception:
        pass

    scen_dir = scenario_dir(sid)
    print("scen_dir_log", scen_dir)
    if not scen_dir.exists():
        return f"[red]scenario '{sid}' not found[/red]"

    # добавить все изменения внутри сценария (git уважает .gitignore)
    repo.git.add(sid)

    # есть ли что коммитить?
    status = repo.git.status("--porcelain", sid)
    if not status.strip():
        return f"[yellow]no changes to push for scenario '{sid}'[/yellow]"

    commit_msg = message or f"Update scenario {sid}"
    repo.index.commit(commit_msg)

    if os.environ.get("ADAOS_TESTING") != "1":
        try:
            repo.remotes.origin.push()
        except Exception as e:
            return f"[red]push failed for scenario '{sid}'[/red] {e}"

    # обновим версию в БД, если есть в scenario.json
    p = scenario_proto_path(sid)
    version = "unknown"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        version = data.get("version", "unknown")
    except Exception:
        pass
    add_or_update_entity("scenario", sid, version, repo.remotes.origin.url, installed=1)

    return f"[green]scenario '{sid}' pushed[/green] (version: {version})"


def install_scenario(sid: str) -> str:
    return pull_scenario(sid)


def uninstall_scenario(sid: str) -> str:
    repo = _git._ensure_repo(_agent.scenarios_dir, _agent.monorepo_scens_url)
    set_installed_flag("scenario", sid, installed=0)
    _git._sync_sparse_checkout(repo, el_list=list_entities("scenario"))
    return f"[green]scenario '{sid}' uninstalled[/green]"


def delete_scenario(sid: str) -> bool:
    d = scenario_dir(sid)
    if not d.exists():
        return False
    # удаляем локально (в репо это будет удаление файлов — по желанию можно коммитить)
    shutil.rmtree(d)
    return True


def read_impl(sid: str, user: str) -> Dict[str, Any]:
    p = impl_dir(sid, user) / "scenario.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_impl(sid: str, user: str, data: Dict[str, Any]) -> Path:
    d = impl_dir(sid, user)
    _ensure_dir(d)
    p = d / "scenario.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_bindings(sid: str, user: str) -> Dict[str, Any]:
    p = bindings_path(sid, user)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"slots": {}, "devices": {}, "secrets": {}}


def write_bindings(sid: str, user: str, data: Dict[str, Any]) -> Path:
    _ensure_dir(bindings_dir(sid))
    p = bindings_path(sid, user)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
