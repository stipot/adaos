# src/adaos/adapters/scenarios/mono_repo.py
from __future__ import annotations
from pathlib import Path
from typing import Optional
import re, yaml

from adaos.domain import SkillId, SkillMeta
from adaos.ports.paths import PathProvider
from adaos.ports.git import GitClient
from adaos.ports.scenarios import ScenarioRepository

MANIFESTS = ("scenario.yaml", "manifest.yaml", "adaos.scenario.yaml")
_name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")


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


class MonoScenarioRepository(ScenarioRepository):
    def __init__(self, *, paths: PathProvider, git: GitClient, url: str, branch: str | None = None):
        self.paths, self.git, self.url, self.branch = paths, git, url, branch

    def _root(self) -> Path:
        return Path(self.paths.scenarios_dir())

    def _ensure(self) -> None:
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

    def get(self, scenario_id: str) -> Optional[SkillMeta]:
        self._ensure()
        p = self._root() / scenario_id
        if p.exists():
            return _read_manifest(p)
        for m in self.list():
            if m.id.value == scenario_id:
                return m
        return None

    def install(self, name: str, *, branch: Optional[str] = None, dest_name: Optional[str] = None) -> SkillMeta:
        self._ensure()
        name = name.strip()
        if not _name_re.match(name):
            raise ValueError("invalid scenario name")
        # В Mono-режиме установка — это включение пути в sparse и pull.
        # Управление набором путей делаем в менеджере (точный set), а здесь просто верификация.
        root = self._root()
        p = _safe_join(root, name)
        # Если папка не подтянута — вернём заглушку после синка
        return _read_manifest(p) if p.exists() else SkillMeta(id=SkillId(name), name=name, version="0.0.0", path=str(p))

    def uninstall(self, scenario_id: str) -> None:
        self._ensure()
        p = _safe_join(self._root(), scenario_id)
        if p.exists():
            import shutil

            shutil.rmtree(p)
