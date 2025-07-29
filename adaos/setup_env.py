import os
import zipfile
import urllib.request
from pathlib import Path
from adaos.db import init_db
from adaos.git_utils import init_git_repo

BASE_DIR = Path(os.getenv("BASE_DIR", str(Path.home())) + "/.adaos")
MODELS_DIR = BASE_DIR / "models"
VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"


def download_vosk_model():
    model_path = MODELS_DIR / VOSK_MODEL_NAME
    if model_path.exists():
        print(f"[AdaOS] Модель Vosk уже установлена: {model_path}")
        return model_path

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = MODELS_DIR / f"{VOSK_MODEL_NAME}.zip"
    print(f"[AdaOS] Скачиваю модель Vosk...")
    urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path)

    print(f"[AdaOS] Распаковываю {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(MODELS_DIR)
    zip_path.unlink()
    return model_path


def prepare_environment():
    # Папки
    (BASE_DIR / "runtime" / "skills").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "runtime" / "tests").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "runtime" / "logs").mkdir(parents=True, exist_ok=True)

    # БД
    os.environ["ADAOS_DB_PATH"] = str(BASE_DIR / "skill_db.sqlite")
    init_db()

    # Git репозиторий навыков
    init_git_repo()

    # Модель wake-word
    download_vosk_model()

    print(f"[AdaOS] Окружение подготовлено в {BASE_DIR}")
