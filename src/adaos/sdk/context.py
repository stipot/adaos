from pathlib import Path
from adaos.sdk.skills.i18n import _
import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

PACKAGE_DIR = Path(__file__).resolve().parent.parent  # adaos/


def _is_android() -> bool:
    # Простая эвристика
    return "ANDROID_BOOTLOGO" in os.environ or os.environ.get("KIVY_BUILD", "") == "android"


def _android_base_dir_fallback() -> Path:
    """Если pyjnius недоступен (например, dev-сборка), используем каталог внутри app/."""
    return Path("./.adaos").resolve()


def _android_base_dir() -> Path:
    try:
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        context = PythonActivity.mActivity
        # Внешнее app-специфичное хранилище (удобно смотреть файлы через проводник)
        ext = context.getExternalFilesDir(None)
        if ext is not None:
            return Path(ext.getAbsolutePath()) / ".adaos"
        # Внутреннее хранилище (не видно пользователю), но точно доступно
        files = context.getFilesDir()
        return Path(files.getAbsolutePath()) / ".adaos"
    except Exception:
        return _android_base_dir_fallback()


def get_base_dir() -> Path:
    # 1) Явная переменная окружения имеет приоритет
    override = os.getenv("ADAOS_BASE_DIR")
    print(f"PACKAGE_DIR: {PACKAGE_DIR.parent.parent}, override {override}, ENV_TYPE: {os.getenv('ENV_TYPE')}")
    if override:
        return Path(override).expanduser().resolve()

    # 2) Android-специфичный путь
    if _is_android():
        return _android_base_dir()

    # 3) Десктоп по-умолчанию (~/.adaos)
    return f"{PACKAGE_DIR.parent.parent}/.adaos" if os.getenv("ENV_TYPE") == "dev" else Path(os.getenv("BASE_DIR") or (Path.home() / ".adaos")).resolve()


# Экспортируем как раньше, чтобы не ломать импорты
BASE_DIR = str(get_base_dir())

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
