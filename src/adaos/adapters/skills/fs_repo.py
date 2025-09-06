from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import yaml

from adaos.domain import SkillId, SkillMeta
from adaos.ports.paths import PathProvider
from adaos.ports.git import GitClient
from adaos.ports.skills import SkillRepository

_MANIFEST_NAMES = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")


def _repo_basename_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _read_manifest(skill_dir: Path) -> tuple[str, str, str]:
    """
    Возвращает (id, name, version).
    Если манифест не найден — падаем в валидные дефолты на основе имени папки.
    """
    for fname in _MANIFEST_NAMES:
        p = skill_dir / fname
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            sid = str(data.get("id") or skill_dir.name)
            name = str(data.get("name") or sid)
            ver = str(data.get("version") or "0.0.0")
            return sid, name, ver
    # дефолт без манифеста
    sid = skill_dir.name
    return sid, sid, "0.0.0"


def _meta_from_dir(path: Path) -> Optional[SkillMeta]:
    if not path.is_dir():
        return None
    sid, name, ver = _read_manifest(path)
    return SkillMeta(id=SkillId(sid), name=name, version=ver, path=str(path.resolve()))


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@")) or s.endswith(".git")


class FsSkillRepository(SkillRepository):
    """
    Простой репозиторий навыков:
      - каждый навык — отдельный git-репозиторий в {skills_dir}/{dest_name or repo_name}
      - манифест в корне: skill.yaml|manifest.yaml|adaos.skill.yaml
    """

    def __init__(self, *, paths: PathProvider, git: GitClient):
        self.paths = paths
        self.git = git

    def _skills_root(self) -> Path:
        return Path(self.paths.skills_dir())

    def list(self) -> list[SkillMeta]:
        root = self._skills_root()
        result: list[SkillMeta] = []
        if not root.exists():
            return result
        for child in sorted(root.iterdir()):
            if child.name.startswith("."):  # пропускаем скрытые
                continue
            meta = _meta_from_dir(child)
            if meta:
                result.append(meta)
        return result

    def get(self, skill_id: str) -> Optional[SkillMeta]:
        root = self._skills_root()
        # быстрый путь: директория в точности совпадает с id
        direct = root / skill_id
        if direct.exists():
            m = _meta_from_dir(direct)
            if m and m.id.value == skill_id:
                return m
        # иначе — поиск по манифестам
        for m in self.list():
            if m.id.value == skill_id:
                return m
        return None

    def install(self, url: str, *, branch: Optional[str] = None, dest_name: Optional[str] = None) -> SkillMeta:
        if not _looks_like_url(url):
            raise ValueError("Ожидаю полный Git URL (multi-repo). " "Похоже, вы в монорежиме — задайте ADAOS_SKILLS_MONOREPO_URL и используйте 'adaos skill install <имя>'.")
        root = self._skills_root()
        root.mkdir(parents=True, exist_ok=True)
        name = dest_name or _repo_basename_from_url(url)
        dest = root / name
        self.git.ensure_repo(str(dest), url, branch=branch)
        # читаем манифест после клона/обновления
        meta = _meta_from_dir(dest)
        if not meta:
            # это очень странно, но подстрахуемся
            meta = SkillMeta(id=SkillId(name), name=name, version="0.0.0", path=str(dest.resolve()))
        return meta

    def remove(self, skill_id: str) -> None:
        meta = self.get(skill_id)
        if not meta:
            raise FileNotFoundError(f"skill '{skill_id}' not found")
        p = Path(meta.path)
        if p.exists():
            shutil.rmtree(p)
