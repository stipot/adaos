import os
import zipfile
import urllib.request
from pathlib import Path
from adaos.db.db import init_db
from adaos.utils.git_utils import init_git_repo
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
    # БД
    init_db()

    # Git репозиторий навыков
    init_git_repo()

    # Модель wake-word
    download_vosk_model()

    print(f"[AdaOS] Окружение подготовлено в {BASE_DIR}")
