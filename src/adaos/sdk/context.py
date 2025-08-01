from pathlib import Path
import os


BASE_DIR = f'{os.getenv("BASE_DIR", str(Path.home()))}/.adaos'
SKILLS_DIR = f"{BASE_DIR}/skills"
PACKAGE_DIR = Path(__file__).resolve().parent.parent  # adaos/
TEMPLATES_DIR = str(PACKAGE_DIR / "skills_templates")
MONOREPO_URL = os.getenv("SKILLS_REPO_URL")
DB_PATH = f"{BASE_DIR}/skill_db.sqlite"
LOCALES_DIR = f"{PACKAGE_DIR}/i18n/locales"
DEFAULT_LANG = "en"

current_skill_path: Path | None = None
current_skill_name: str = ""


def set_current_skill(skill_name: str):
    global current_skill_path, current_skill_name
    current_skill_path = os.path.join(Path(SKILLS_DIR), skill_name)
    if not os.path.exists(current_skill_path):
        return f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]"
    print("point1", type(current_skill_path), current_skill_path)
    current_skill_name = skill_name


def set_current_skill_path(path: Path):
    global current_skill_path
    current_skill_path = path


def get_current_skill_path() -> Path:
    if current_skill_path is None:
        raise RuntimeError("Skill path not set in context")
    return current_skill_path
