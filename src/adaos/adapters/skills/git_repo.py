from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

import yaml

from adaos.domain import SkillId, SkillMeta
from adaos.ports.paths import PathProvider
from adaos.ports.git import GitClient
from adaos.ports.skills import SkillRepository

try:
    from adaos.services.fs.safe_io import remove_tree  # мягкое удаление, если доступно
except Exception:  # pragma: no cover
    remove_tree = None  # fallback на shutil.rmtree

_MANIFEST_NAMES = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")
_CATALOG_FILE = "skills.yaml"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


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


def _read_catalog(paths: PathProvider) -> list[str]:
    # ищем skills.yaml рядом с базой/директорией навыков (первое найденное)
    candidates: list[Path] = []
    base = getattr(paths, "base", None)
    if base:
        candidates.append(Path(base) / _CATALOG_FILE)
    skills_dir = Path(paths.skills_dir())
    candidates.extend([skills_dir.parent / _CATALOG_FILE, skills_dir / _CATALOG_FILE])
    for c in candidates:
        if c.exists():
            y = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
            items = y.get("skills") or []
            return [str(s).strip() for s in items if str(s).strip()]
    return []


@dataclass
class GitSkillRepository(SkillRepository):
    """
    Унифицированный адаптер навыков:
      - monorepo mode: если задан monorepo_url (и опц. monorepo_branch)
      - fs mode (multi-repo): если monorepo_url не задан
    """

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

    # --- common

    def _root(self) -> Path:
        return Path(self.paths.skills_dir())

    def _ensure_monorepo(self) -> None:
        if os.getenv("ADAOS_TESTING") == "1":
            return
        self.git.ensure_repo(str(self._root()), self.monorepo_url, branch=self.monorepo_branch)

    def ensure(self) -> None:
        if self.monorepo_url:
            self._ensure_monorepo()
        else:
            self._root().mkdir(parents=True, exist_ok=True)

    # --- listing / get

    def list(self) -> list[SkillMeta]:
        self.ensure()
        result: List[SkillMeta] = []
        root = self._root()
        if not root.exists():
            return result
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            meta = _read_manifest(child)
            result.append(meta)
        return result

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        self.ensure()
        root = self._root()
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
        """
        monorepo mode: ref = skill name (подкаталог); URL запрещён.
        fs mode:      ref = git URL; dest_name опционален.
        """
        self.ensure()
        root = self._root()

        if self.monorepo_url:
            # monorepo: ожидаем имя скилла из каталога
            name = ref.strip()
            if not _NAME_RE.match(name):
                raise ValueError("invalid skill name")
            """ catalog = set(_read_catalog(self.paths))
            if catalog and name not in catalog:
                raise ValueError(f"skill '{name}' not found in catalog") """
            # sparse checkout только нужного подкаталога
            self.git.sparse_init(str(root), cone=False)
            self.git.sparse_add(str(root), name)
            self.git.pull(str(root))
            p = _safe_join(root, name)
            if not p.exists():
                raise FileNotFoundError(f"skill '{name}' not present after sync")
            return _read_manifest(p)

        # fs mode: ожидаем полный URL
        if not _looks_like_url(ref):
            raise ValueError("ожидаю полный Git URL (multi-repo). " "если вы в монорежиме — задайте ADAOS_SKILLS_MONOREPO_URL и вызывайте install(<skill_name>)")
        root.mkdir(parents=True, exist_ok=True)
        name = dest_name or _repo_basename_from_url(ref)
        dest = root / name
        self.git.ensure_repo(str(dest), ref, branch=branch)
        return _read_manifest(dest)

    def uninstall(self, skill_id: str) -> None:
        self.ensure()
        p = _safe_join(self._root(), skill_id)
        if not p.exists():
            raise FileNotFoundError(f"skill '{skill_id}' not found")
        if remove_tree:
            remove_tree(str(p), fs=getattr(self.paths, "ctx", None).fs if getattr(self.paths, "ctx", None) else None)  # type: ignore[attr-defined]
        else:
            shutil.rmtree(p)
