from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from adaos.services.agent_context import AgentContext


@dataclass(slots=True)
class SkillUpdateResult:
    updated: bool
    version: Optional[str]


@dataclass(slots=True)
class SkillUpdateService:
    ctx: AgentContext

    def request_update(self, skill_id: str, *, dry_run: bool = False) -> SkillUpdateResult:
        repo = self.ctx.skills_repo
        meta = repo.get(skill_id)
        if meta is None:
            raise FileNotFoundError(f"skill '{skill_id}' is not installed")

        root = Path(self.ctx.paths.skills_dir())
        skill_path = Path(getattr(meta, "path", root / skill_id))
        version = getattr(meta, "version", None)

        if dry_run:
            return SkillUpdateResult(updated=False, version=version)

        fs = getattr(self.ctx, "fs", None)
        if fs and hasattr(fs, "require_write"):
            try:
                fs.require_write(str(skill_path))
            except Exception as exc:
                raise PermissionError("fs.readonly") from exc
        settings = getattr(self.ctx, "settings", None)
        git = getattr(self.ctx, "git", None)
        if git is None:
            raise RuntimeError("Git client is not configured")

        if settings and getattr(settings, "skills_monorepo_url", None):
            if fs and hasattr(fs, "require_write"):
                try:
                    fs.require_write(str(root))
                except Exception as exc:
                    raise PermissionError("fs.readonly") from exc
            git.sparse_add(str(root), skill_id)
            git.pull(str(root))
        else:
            git.pull(str(skill_path))

        refreshed = repo.get(skill_id) or meta
        return SkillUpdateResult(updated=True, version=getattr(refreshed, "version", version))
