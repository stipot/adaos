from __future__ import annotations

import os
import re
import shutil
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from adaos.adapters.git.workspace import SparseWorkspace, wait_for_materialized
from adaos.services.workspace_registry import (
    format_workspace_registry_not_found,
    registry_pattern_set,
    resolve_workspace_registry_install_name,
)
from adaos.services.git.availability import get_git_availability
from adaos.services.git.archive import materialize_subpath_from_github_zip
from adaos.domain import SkillId, SkillMeta
from adaos.ports.git import GitClient
from adaos.ports.paths import PathProvider
from adaos.ports.skills import SkillRepository

try:
    from adaos.services.fs.safe_io import remove_tree  # мягкое удаление, если доступно
except Exception:  # pragma: no cover
    remove_tree = None  # fallback на shutil.rmtree

_MANIFEST_NAMES = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")
_CATALOG_FILE = "skills.yaml"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\/]+$")
_log = logging.getLogger(__name__)


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@")) or s.endswith(".git")


def _repo_basename_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "skill"


def _safe_join(root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise ValueError("unsafe path traversal (absolute)")
    p = (root / rel_path).resolve()
    root = root.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("unsafe path traversal")
    return p


def _read_manifest(skill_dir: Path) -> SkillMeta:
    for fname in _MANIFEST_NAMES:
        p = skill_dir / fname
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            sid = str(data.get("id") or skill_dir.name)
            name = str(data.get("name") or sid)
            ver = str(data.get("version") or "0.0.0")
            return SkillMeta(id=SkillId(sid), name=name, version=ver, path=str(skill_dir.resolve()))
    # дефолты, если манифеста нет
    sid = skill_dir.name
    return SkillMeta(id=SkillId(sid), name=sid, version="0.0.0", path=str(skill_dir.resolve()))


@dataclass
class GitSkillRepository(SkillRepository):
    """Skill repository backed by a monorepo workspace with sparse-checkout."""

    def __init__(
        self,
        *,
        paths: PathProvider,
        git: GitClient,
        monorepo_url: Optional[str] = None,
        monorepo_branch: Optional[str] = None,
    ):
        self.paths = paths
        self.git = git
        self.monorepo_url = monorepo_url
        self.monorepo_branch = monorepo_branch

    def _candidate_roots(self) -> list[Path]:
        roots: list[Path] = []
        primary = Path(self.paths.skills_dir())
        roots.append(primary)
        cache_attr = getattr(self.paths, "skills_cache_dir", None)
        if cache_attr:
            cache_root = cache_attr() if callable(cache_attr) else cache_attr
            if cache_root:
                roots.append(Path(cache_root) / "skills")
        uniq: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                uniq.append(root)
        return uniq

    def _ensure_monorepo(self) -> None:
        if os.getenv("ADAOS_TESTING") == "1":
            return
        if not self.monorepo_url:
            return
        av = get_git_availability(base_dir=self.paths.base_dir())
        if av.enabled and av.git_path:
            self.git.ensure_repo(str(self.paths.workspace_dir()), self.monorepo_url, branch=self.monorepo_branch)
            return
        # No-git mode: keep workspace as plain directory; skills are materialized from archive on demand.
        self.paths.workspace_dir().mkdir(parents=True, exist_ok=True)

    def ensure(self) -> None:
        if self.monorepo_url:
            self._ensure_monorepo()
        else:
            self.paths.workspace_dir().mkdir(parents=True, exist_ok=True)

    def resolve_install_name(self, ref: str) -> str:
        self.ensure()
        name = ref.strip()
        if not _NAME_RE.match(name):
            raise ValueError("invalid skill name")

        workspace_root = self.paths.workspace_dir()
        av = get_git_availability(base_dir=self.paths.base_dir())
        if av.enabled and av.git_path and (workspace_root / ".git").exists():
            sparse = SparseWorkspace(self.git, workspace_root)
            sparse.update(add=registry_pattern_set([]))
            try:
                self.git.pull(str(workspace_root))
            except Exception:
                pass
            resolved, entry = resolve_workspace_registry_install_name(
                workspace_root,
                kind="skills",
                name_or_id=name,
                fallback_to_scan=False,
            )
            if entry is None:
                raise FileNotFoundError(
                    format_workspace_registry_not_found(
                        workspace_root,
                        kind="skills",
                        name_or_id=name,
                        fallback_to_scan=False,
                    )
                )
            if resolved != name:
                _log.warning(
                    "skill install request resolved through registry repo=%s request=%s name=%s id=%s version=%s",
                    str(workspace_root),
                    name,
                    resolved,
                    str(entry.get("id") or ""),
                    str(entry.get("version") or ""),
                )
            return resolved
        return name

    # --- listing / get

    def list(self) -> list[SkillMeta]:
        self.ensure()
        result: List[SkillMeta] = []
        for root in self._candidate_roots():
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                meta = _read_manifest(child)
                result.append(meta)
        return result

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        self.ensure()
        for root in self._candidate_roots():
            direct = root / skill_id
            if direct.exists():
                m = _read_manifest(direct)
                if m and m.id.value == skill_id:
                    return m
        for m in self.list():
            if m.id.value == skill_id:
                return m
        return None

    # --- install

    def install(
        self,
        ref: str,
        *,
        branch: Optional[str] = None,
        dest_name: Optional[str] = None,
    ) -> SkillMeta:
        """Install a skill from monorepo: ensure sparse checkout and pull the subdir."""

        self.ensure()
        name = ref.strip()
        if not _NAME_RE.match(name):
            raise ValueError("invalid skill name")

        workspace_root = self.paths.workspace_dir()
        name = self.resolve_install_name(name)
        target = f"skills/{name}"

        av = get_git_availability(base_dir=self.paths.base_dir())
        used_sparse = False
        if av.enabled and av.git_path and (workspace_root / ".git").exists():
            sparse = SparseWorkspace(self.git, workspace_root)
            sparse.update(add=registry_pattern_set([target]))
            used_sparse = True
        else:
            if not self.monorepo_url:
                raise RuntimeError("skills monorepo is not configured (ADAOS_SKILLS_MONOREPO_URL)")
            b = (branch or self.monorepo_branch or "").strip()
            if not b:
                raise RuntimeError("skills monorepo branch is not configured (ADAOS_SKILLS_MONOREPO_BRANCH)")
            materialize_subpath_from_github_zip(
                repo_url=self.monorepo_url,
                branch=b,
                dest_root=workspace_root,
                subpath=target,
                clean=True,
            )
        # Аналогично сценариям: при установке навыка не падаем, если git pull
        # не может выполниться из‑за локальных незакоммиченных изменений в workspace.
        # Если навык после этого не появится на диске, будет брошена FileNotFoundError ниже.
        if used_sparse:
            try:
                self.git.pull(str(workspace_root))
            except Exception:
                pass

        skill_dir: Path = self.paths.skills_dir() / name
        try:
            wait_for_materialized(skill_dir, files=_MANIFEST_NAMES)
        except FileNotFoundError as exc:  # pragma: no cover - defensive logging
            if used_sparse:
                try:
                    sparse.update(remove=[target])
                    self.git.rm_cached(str(workspace_root), target)
                except Exception:
                    pass
            raise FileNotFoundError(f"skill '{name}' not present after sync") from exc
        return _read_manifest(skill_dir)

    def uninstall(self, skill_id: str) -> None:
        self.ensure()
        workspace_root = self.paths.workspace_dir()
        target = f"skills/{skill_id}"
        try:
            if (workspace_root / ".git").exists():
                sparse = SparseWorkspace(self.git, workspace_root)
                sparse.update(remove=[target])
                self.git.rm_cached(str(workspace_root), target)
        except Exception:
            pass

        p: Path = self.paths.skills_dir() / skill_id
        if not p.exists():
            return

        if remove_tree:
            remove_tree(
                str(p),
                fs=getattr(self.paths, "ctx", None).fs if getattr(self.paths, "ctx", None) else None,
            )  # type: ignore[attr-defined]
        else:
            shutil.rmtree(p)
        if p.exists():  # pragma: no cover - defensive fallback
            raise FileExistsError(f"skill '{skill_id}' still present after uninstall")
