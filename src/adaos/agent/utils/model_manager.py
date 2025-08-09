# -*- coding: utf-8 -*-
"""
Загрузка и распаковка офлайн‑моделей (Vosk).
Без внешних зависимостей: urllib + zipfile.
"""

from __future__ import annotations
import os
import sys
import shutil
import zipfile
import tempfile
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

# Минимальные пресеты (можно расширять)
LANG_PRESETS = {
    # EN
    "en": {
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
        "folder": "vosk-model-small-en-us-0.15",
        "target": "en-us",
    },
    "en-us": {
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
        "folder": "vosk-model-small-en-us-0.15",
        "target": "en-us",
    },
    # RU
    "ru": {
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        "folder": "vosk-model-small-ru-0.22",
        "target": "ru-ru",
    },
    "ru-ru": {
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        "folder": "vosk-model-small-ru-0.22",
        "target": "ru-ru",
    },
}


def _print_progress(downloaded: int, total: Optional[int]) -> None:
    if not sys.stderr.isatty() or total is None or total <= 0:
        return
    width = 40
    frac = min(1.0, downloaded / total)
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    percent = int(frac * 100)
    sys.stderr.write(f"\r[download] |{bar}| {percent:3d}%")
    sys.stderr.flush()
    if downloaded >= total:
        sys.stderr.write("\n")


def _download_zip(url: str, dest_zip: Path) -> None:
    with urlopen(url) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk = 1024 * 128
        with open(dest_zip, "wb") as f:
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                f.write(data)
                downloaded += len(data)
                _print_progress(downloaded, total)


def _extract_zip(src_zip: Path, dest_dir: Path) -> None:
    with zipfile.ZipFile(src_zip, "r") as zf:
        zf.extractall(dest_dir)


def ensure_vosk_model(lang: str = "en", base_dir: Path | str = "models/vosk") -> Path:
    """
    Проверяет наличие модели; если нет — скачивает и распаковывает.
    Возвращает путь к папке модели: base_dir/<target>
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    key = lang.lower().strip()
    preset = LANG_PRESETS.get(key)
    if not preset:
        # дефолт — en
        preset = LANG_PRESETS["en"]
        key = "en"

    target = base / preset["target"]
    if target.exists() and any(target.iterdir()):
        return target

    url = preset["url"]
    folder_in_zip = preset["folder"]

    # Скачиваем во временный файл
    tmp_dir = Path(tempfile.mkdtemp(prefix="adaos_vosk_"))
    try:
        zip_path = tmp_dir / "model.zip"
        print(f"[Vosk] Модель не найдена, скачиваю:\n{url}")
        _download_zip(url, zip_path)
        print(f"\n[Vosk] Распаковываю в {base.resolve()}")
        _extract_zip(zip_path, base)
        unpacked = base / folder_in_zip
        if not unpacked.exists():
            # иногда архив распаковывается иначе — проверим первый уровень
            candidates = [p for p in base.iterdir() if p.is_dir() and p.name.startswith("vosk-model")]
            if candidates:
                unpacked = max(candidates, key=lambda p: p.stat().st_mtime)

        # Переименуем в целевой алиас (en-us / ru-ru)
        if target.exists():
            shutil.rmtree(target)
        unpacked.rename(target)
        print(f"[Vosk] Готово: {target.resolve()}")
        return target
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
