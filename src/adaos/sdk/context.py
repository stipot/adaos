from pathlib import Path
from adaos.sdk.skills.i18n import _
import os


PACKAGE_DIR = Path(__file__).resolve().parent.parent  # adaos/
BASE_DIR = f'{os.getenv("BASE_DIR", PACKAGE_DIR.parent.parent)}/.adaos'  # str(Path.home())
SKILLS_DIR = f"{BASE_DIR}/skills"
TEMPLATES_DIR = str(PACKAGE_DIR / "skills_templates")
MONOREPO_URL = os.getenv("SKILLS_REPO_URL", "https://github.com/stipot/adaoskills.git")
DB_PATH = f"{BASE_DIR}/skill_db.sqlite"
LOCALES_DIR = f"{PACKAGE_DIR}/sdk/locales"
DEFAULT_LANG = "en"
ADAOS_VOSK_MODEL = f"{BASE_DIR}/models/vosk/en-us"

current_skill_path: Path | None = None
current_skill_name: str = ""


def set_current_skill(skill_name: str):
    global current_skill_path, current_skill_name
    current_skill_path = os.path.join(Path(SKILLS_DIR), skill_name)
    if not os.path.exists(current_skill_path):
        return f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]"
    current_skill_name = skill_name


def set_current_skill_path(path: Path):
    global current_skill_path
    current_skill_path = path


def get_current_skill_path() -> Path:
    if current_skill_path is None:
        raise RuntimeError("Skill path not set in context")
    return current_skill_path
