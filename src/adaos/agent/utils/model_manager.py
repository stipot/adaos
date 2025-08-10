# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import shutil
import zipfile
import tempfile
from pathlib import Path
from typing import Optional, List
import typer
from urllib.request import urlopen, Request
from urllib.error import URLError

LANG_PRESETS = {
    "en": {
        "urls": [
            # основной
            "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
            # зеркала можно добавлять сюда
        ],
        "folder": "vosk-model-small-en-us-0.15",
        "target": "en-us",
    },
    "en-us": {
        "urls": [
            "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
        ],
        "folder": "vosk-model-small-en-us-0.15",
        "target": "en-us",
    },
    "ru": {
        "urls": [
            "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        ],
        "folder": "vosk-model-small-ru-0.22",
        "target": "ru-ru",
    },
    "ru-ru": {
        "urls": [
            "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        ],
        "folder": "vosk-model-small-ru-0.22",
        "target": "ru-ru",
    },
}


def _print_progress(downloaded: int, total: Optional[int]) -> None:
    if not sys.stderr.isatty() or not total:
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
    req = Request(url, headers={"User-Agent": "AdaOS/1.0"})
    with urlopen(req, timeout=60) as resp:
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


def _possible_urls(preset_urls: List[str]) -> List[str]:
    # Позволяем переопределить зеркало через переменную окружения
    mirror = os.environ.get("ADAOS_VOSK_MIRROR")
    if mirror:
        return [mirror] + preset_urls
    return preset_urls


def ensure_vosk_model(lang: str = "en", base_dir: Path | str = "models/vosk", local_zip: Optional[Path | str] = None) -> Path:
    """
    Гарантирует наличие модели Vosk.
    - Если local_zip задан (или переменная ADAOS_VOSK_ZIP) — распаковывает её.
    - Иначе пытается скачать с набора URL (можно переопределить зеркалом ADAOS_VOSK_MIRROR).
    Возвращает путь к папке модели: base_dir/<target>
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    key = lang.lower().strip()
    preset = LANG_PRESETS.get(key) or LANG_PRESETS["en"]
    target = base / preset["target"]
    if target.exists() and any(target.iterdir()):
        return target

    # 1) если дали локальный ZIP — используем его
    env_zip = os.environ.get("ADAOS_VOSK_ZIP")
    local_zip_path = Path(local_zip or env_zip) if (local_zip or env_zip) else None
    if local_zip_path:
        if not local_zip_path.exists():
            raise FileNotFoundError(f"ZIP не найден: {local_zip_path}")
        print(f"[Vosk] Распаковываю локальный архив: {local_zip_path}")
        _extract_zip(local_zip_path, base)
    else:
        # 2) пробуем скачать по списку URL
        urls = _possible_urls(preset["urls"])
        last_err: Optional[Exception] = None
        tmp_dir = Path(tempfile.mkdtemp(prefix="adaos_vosk_"))
        try:
            zip_path = tmp_dir / "model.zip"
            for url in urls:
                try:
                    print(f"[Vosk] Модель не найдена, скачиваю:\n{url}")
                    _download_zip(url, zip_path)
                    print(f"\n[Vosk] Распаковываю в {base.resolve()}")
                    _extract_zip(zip_path, base)
                    last_err = None
                    break
                except URLError as e:
                    last_err = e
                    print(f"[Vosk] Не удалось скачать с {url}: {e}")
                except Exception as e:
                    last_err = e
                    print(f"[Vosk] Ошибка при загрузке {url}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if last_err:
            # дружелюбная подсказка
            raise RuntimeError(
                "Не получилось автоматически скачать Vosk‑модель.\n"
                "Что можно сделать:\n"
                "  1) Указать путь к уже распакованной модели флагом --model\n"
                "  2) Задать локальный zip через --model-zip или переменную ADAOS_VOSK_ZIP\n"
                "  3) Указать зеркало через ADAOS_VOSK_MIRROR\n"
                "  4) Проверить прокси: переменные HTTPS_PROXY/HTTP_PROXY\n"
                f"Исходная ошибка: {last_err}"
            )

    # после распаковки — найти распакованную папку
    folder_in_zip = preset["folder"]
    unpacked = base / folder_in_zip
    if not unpacked.exists():
        # fallback: ищем первую папку vosk-model*
        candidates = [p for p in base.iterdir() if p.is_dir() and p.name.startswith("vosk-model")]
        if candidates:
            # берём самую свежую
            unpacked = max(candidates, key=lambda p: p.stat().st_mtime)

    # Переименуем в целевой алиас (en-us / ru-ru)
    if target.exists():
        shutil.rmtree(target)
    unpacked.rename(target)
    print(f"[Vosk] Готово: {target.resolve()}")
    return target
