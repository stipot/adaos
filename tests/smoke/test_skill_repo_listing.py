# tests/smoke/test_skill_repo_listing.py
from pathlib import Path
from adaos.apps.bootstrap import init_ctx
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.domain import SkillId


def test_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
    ctx = init_ctx()
    repo = GitSkillRepository(paths=ctx.paths, git=ctx.git)
    assert repo.list() == []


def test_manifest_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
    ctx = init_ctx()
    sd = Path(ctx.paths.skills_dir()) / "foo"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "skill.yaml").write_text("id: demo\nversion: '1.2.3'\nname: Demo Skill\n", encoding="utf-8")
    from adaos.services.skill.service import SkillService

    svc = SkillService(repo=GitSkillRepository(paths=ctx.paths, git=ctx.git), bus=ctx.bus)
    items = svc.list()
    assert any(m.id.value == "demo" and m.version == "1.2.3" for m in items)
