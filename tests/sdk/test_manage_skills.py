from __future__ import annotations

from dataclasses import dataclass

import pytest

from adaos.domain import SkillId, SkillMeta
from adaos.sdk.errors import CapabilityError
from adaos.sdk.manage import skills_install, skills_list, skills_uninstall
from adaos.services.agent_context import get_ctx


class AllowCaps:
    def __init__(self, allowed: set[str]):
        self._allowed = allowed

    def allows(self, capability: str) -> bool:
        return capability in self._allowed


@dataclass
class FakeSkillRepo:
    installed: dict[str, SkillMeta]

    def ensure(self) -> None:
        pass

    def get(self, skill_id: str) -> SkillMeta | None:
        return self.installed.get(skill_id)

    def install(self, ref: str, *, branch: str | None = None, dest_name: str | None = None) -> SkillMeta:
        skill_id = dest_name or ref
        meta = SkillMeta(id=SkillId(skill_id), name=skill_id, version="1.0.0", path=f"/skills/{skill_id}")
        self.installed[skill_id] = meta
        return meta

    def uninstall(self, skill_id: str) -> None:
        if skill_id not in self.installed:
            raise FileNotFoundError(skill_id)
        del self.installed[skill_id]

    def list(self) -> list[SkillMeta]:
        return list(self.installed.values())


@pytest.fixture
def skill_repo():
    ctx = get_ctx()
    repo = FakeSkillRepo(installed={})
    object.__setattr__(ctx, "_skills_repo", repo)
    return repo


def test_skills_install_dry_run(skill_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.skills.install"})
    res = skills_install("weather", request_id="req-install", dry_run=True)
    assert res["status"] == "ok"
    assert res["action"] == "install"
    assert res["dry_run"] is True


def test_skills_install_idempotent(skill_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.skills.install"})
    result = skills_install("weather", request_id="req-real")
    repeat = skills_install("weather", request_id="req-real")
    assert repeat == result
    assert "weather" in skill_repo.installed


def test_skills_uninstall(skill_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.skills.install", "manage.skills.uninstall"})
    skills_install("weather", request_id="req-install2")

    res = skills_uninstall("weather", request_id="req-uninstall")
    assert res["action"] == "removed"
    assert skills_uninstall("weather", request_id="req-uninstall") == res


def test_skills_list(skill_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.skills.list", "manage.skills.install"})
    skills_install("one", request_id="req-one")
    skills_install("two", request_id="req-two")

    listing = skills_list()
    ids = sorted(item["id"] for item in listing["skills"])
    assert ids == ["one", "two"]


def test_skills_install_missing_capability(skill_repo):
    ctx = get_ctx()
    ctx.caps = AllowCaps(set())
    with pytest.raises(CapabilityError):
        skills_install("weather", request_id="req-no-cap")
