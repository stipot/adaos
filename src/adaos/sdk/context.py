# src\adaos\sdk\context.py
from pathlib import Path
from adaos.sdk.skills.i18n import _
import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

PACKAGE_DIR = Path(__file__).resolve().parent.parent  # adaos/


class EnvironmentContext:
    def __init__(self):
        self.package_dir = Path(__file__).resolve().parent.parent  # adaos/
        self.env_type = os.getenv("ENV_TYPE", "prod")
        self.override_base = os.getenv("ADAOS_BASE_DIR")

    def is_android(self) -> bool:
        return "ANDROID_BOOTLOGO" in os.environ or os.getenv("KIVY_BUILD", "") == "android"

    def _android_base_dir(self) -> Path:
        try:
            from jnius import autoclass

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            context = PythonActivity.mActivity
            ext = context.getExternalFilesDir(None)
            if ext is not None:
                return Path(ext.getAbsolutePath()) / ".adaos"
            return Path(context.getFilesDir().getAbsolutePath()) / ".adaos"
        except Exception:
            return Path("./.adaos").resolve()

    def get_base_dir(self) -> Path:
        if self.override_base:
            return Path(self.override_base).expanduser().resolve()
        if self.is_android():
            return self._android_base_dir()
        if self.env_type == "dev":
            return self.package_dir.parent.parent / ".adaos"
        return Path(os.getenv("BASE_DIR") or (Path.home() / ".adaos")).resolve()


class AgentContext:
    def __init__(self, env: EnvironmentContext):
        self.env = env
        self.base_dir = env.get_base_dir()
        self.skills_dir = self.base_dir / "skills"
        self.templates_dir = self.env.package_dir / "skills_templates"
        self.db_path = self.base_dir / "skill_db.sqlite"
        self.locales_dir = self.env.package_dir / "sdk/locales"
        self.models_dir = self.base_dir / "models"

        self.monorepo_url = os.getenv("SKILLS_REPO_URL", "https://github.com/stipot/adaoskills.git")
        self.default_lang = os.getenv("ADAOS_DEFAULT_LANG", "en")

    def get_skill_context(self, skill_name: str) -> "SkillContext":
        return SkillContext(skill_name, self.skills_dir / skill_name)


class SkillContext:
    def __init__(self, name: str, path: Path):
        self.name = name
        self.path = path
        if not self.path.exists():
            raise FileNotFoundError(f"Skill {name} not found at {path}")

    @property
    def locales_dir(self) -> Path:
        return self.path / "locales"

    @property
    def config_path(self) -> Path:
        return self.path / "config.json"


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
    if override:
        return Path(override).expanduser().resolve()

    # 2) Android-специфичный путь
    if _is_android():
        return _android_base_dir()

    # 3) Десктоп по-умолчанию (~/.adaos)
    return Path(f"{PACKAGE_DIR.parent.parent}/.adaos").resolve() if os.getenv("ENV_TYPE") == "dev" else Path(os.getenv("BASE_DIR") or (Path.home() / ".adaos")).resolve()


_env = EnvironmentContext()
_agent = AgentContext(_env)

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
    global _current_skill
    try:
        _current_skill = _agent.get_skill_context(skill_name)
    except FileNotFoundError:
        from adaos.sdk.skills.i18n import _

        return f"[red]{_('skill.not_found', skill_name=skill_name)}[/red]"


def set_current_skill_path(path: Path):
    global _current_skill
    _current_skill = SkillContext(path.name, path)


def get_current_skill_path() -> Path:
    if _current_skill is None:
        raise RuntimeError("Skill path not set in context")
    return _current_skill.path


def get_current_skill() -> SkillContext:
    if _current_skill is None:
        raise RuntimeError("No current skill set")
    return _current_skill
