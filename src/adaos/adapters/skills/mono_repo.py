# src/adaos/adapters/skills/mono_repo.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Iterable
import re, yaml, os

from adaos.domain import SkillId, SkillMeta
from adaos.ports.paths import PathProvider
from adaos.ports.git import GitClient
from adaos.ports.skills import SkillRepository
from adaos.services.fs.safe_io import remove_tree

CATALOG_FILE = "skills.yaml"
MANIFESTS = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")

_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


def _safe_join(root: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise ValueError("unsafe path traversal (absolute)")
    p = (root / rel_path).resolve()
    root = root.resolve()
    try:
        # выбросит ValueError, если p не внутри root
        p.relative_to(root)
    except ValueError:
        raise ValueError("unsafe path traversal")
    return p


def _read_manifest(p: Path) -> SkillMeta:
    for fn in MANIFESTS:
        f = p / fn
        if f.exists():
            y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            sid = str(y.get("id") or p.name)
            name = str(y.get("name") or sid)
            ver = str(y.get("version") or "0.0.0")
            return SkillMeta(id=SkillId(sid), name=name, version=ver, path=str(p))
    return SkillMeta(id=SkillId(p.name), name=p.name, version="0.0.0", path=str(p))


def _read_catalog(paths: PathProvider) -> list[str]:
    # ищем skills.yaml рядом с base или внутри монорепо (первое найденное)
    candidates = [
        Path(paths.base) / CATALOG_FILE,
        Path(paths.skills_dir()).parent / CATALOG_FILE,
        Path(paths.skills_dir()) / CATALOG_FILE,
    ]
    for c in candidates:
        if c.exists():
            y = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
            items = y.get("skills") or []
            return [str(s).strip() for s in items if str(s).strip()]
    return []


class MonoSkillRepository(SkillRepository):
    def __init__(self, *, paths: PathProvider, git: GitClient, url: str, branch: str | None = None):
        self.paths, self.git, self.url, self.branch = paths, git, url, branch

    def _root(self) -> Path:
        return Path(self.paths.skills_dir())

    def _ensure(self) -> None:
        if os.getenv("ADAOS_TESTING") == "1":
            return None
        self.git.ensure_repo(str(self._root()), self.url, branch=self.branch)

    def ensure(self) -> None:
        self._ensure()

    def list(self) -> list[SkillMeta]:
        self._ensure()
        items: list[SkillMeta] = []
        for ch in sorted(self._root().iterdir()):
            if ch.is_dir() and not ch.name.startswith("."):
                items.append(_read_manifest(ch))
        return items

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        self._ensure()
        p = self._root() / skill_id
        if p.exists():
            return _read_manifest(p)
        for m in self.list():
            if m.id.value == skill_id:
                return m
        return None

    def install(self, name: str, *, branch: str | None = None, dest_name: str | None = None) -> SkillMeta:
        # только имена, которые перечислены в каталоге
        self._ensure()
        name = name.strip()
        if not _name_re.match(name):  # защита от инъекций путей
            raise ValueError("invalid skill name")
        catalog = set(_read_catalog(self.paths))
        if catalog and name not in catalog:
            raise ValueError(f"skill '{name}' not found in catalog")
        # sparse + pull
        self.git.sparse_init(str(self._root()), cone=False)
        self.git.sparse_add(str(self._root()), name)
        self.git.pull(str(self._root()))
        p = _safe_join(self._root(), name)
        if not p.exists():
            raise FileNotFoundError(f"skill '{name}' not present after sync")
        return _read_manifest(p)

    def uninstall(self, skill_id: str) -> None:
        self._ensure()
        p = _safe_join(self._root(), skill_id)
        remove_tree(str(p), fs=self.paths.ctx.fs)
