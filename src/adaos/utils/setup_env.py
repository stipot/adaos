import os
import zipfile
import urllib.request
import shutil
from pathlib import Path
from adaos.db.sqlite import init_db
from adaos.utils.git_utils import _ensure_repo
from adaos.sdk.context import BASE_DIR, DB_PATH

MODELS_DIR = f"{BASE_DIR}/models"
VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"


def download_vosk_model():
    model_path = Path(MODELS_DIR) / VOSK_MODEL_NAME
    if model_path.exists():
        print(f"[AdaOS] Модель Vosk уже установлена: {model_path}")
        return model_path

    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
    zip_path = Path(MODELS_DIR) / f"{VOSK_MODEL_NAME}.zip"
    print(f"[AdaOS] Скачиваю модель Vosk...")
    urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path)

    print(f"[AdaOS] Распаковываю {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(Path(MODELS_DIR))
    zip_path.unlink()
    return model_path


def prepare_environment():
    env_sample = Path(__file__).parent.parent / ".env.sample"
    env_file = Path(BASE_DIR) / ".env"

    if not env_file.exists() and env_sample.exists():
        shutil.copy(env_sample, env_file)
        print(f"[AdaOS] Created .env file at {env_file}")

    # БД
    init_db()

    # Git репозиторий навыков
    _ensure_repo()

    # Модель wake-word
    download_vosk_model()

    print(f"[AdaOS] Окружение подготовлено в {BASE_DIR}")
