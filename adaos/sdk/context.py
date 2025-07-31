from pathlib import Path

current_skill_path: Path | None = None


def set_current_skill_path(path: Path):
    global current_skill_path
    current_skill_path = path


def get_current_skill_path() -> Path:
    if current_skill_path is None:
        raise RuntimeError("Skill path not set in context")
    return current_skill_path
