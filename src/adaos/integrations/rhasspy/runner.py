# src/adaos/integrations/rhasspy_adapter/runner.py
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional
import typer

import json

try:
    import requests  # легковесная зависимость для healthcheck
except Exception:
    requests = None  # type: ignore


DEFAULT_CONTAINER = os.getenv("ADAOS_RHASSPY_CONTAINER", "adaos-rhasspy")
DEFAULT_IMAGE = os.getenv("ADAOS_RHASSPY_IMAGE", "rhasspy/rhasspy:latest")
DEFAULT_PORT = int(os.getenv("ADAOS_RHASSPY_PORT", "12101"))
DEFAULT_PROFILE = os.getenv("ADAOS_RHASSPY_PROFILE", "en")
DEFAULT_DATA_DIR = os.getenv("ADAOS_RHASSPY_DATA", str(Path.home() / ".config" / "rhasspy"))


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False


def start_rhasspy(
    container_name: str = DEFAULT_CONTAINER, image: str = DEFAULT_IMAGE, port: int = DEFAULT_PORT, profile: str = DEFAULT_PROFILE, data_dir: str = DEFAULT_DATA_DIR
) -> bool:
    """Старт Rhasspy в контейнере. Возвращает True, если команда отдана успешно."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    if not _docker_available():
        print("[Rhasspy Runner] Docker недоступен. Установите Docker или запустите Rhasspy вручную.")
        return False

    # пробуем запустить/перезапустить контейнер
    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    cmd = ["docker", "run", "-d", "--name", container_name, "-p", f"{port}:12101", "-v", f"{data_dir}:/profiles", image, "--user-profiles", "/profiles", "--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("[Rhasspy Runner] Не удалось запустить контейнер:\n", proc.stderr.strip())
        return False

    print(f"[Rhasspy Runner] Стартует контейнер {container_name} на http://127.0.0.1:{port}")
    return True


def stop_rhasspy(container_name: str = DEFAULT_CONTAINER) -> None:
    if not _docker_available():
        print("[Rhasspy Runner] Docker недоступен.")
        return
    subprocess.run(["docker", "rm", "-f", container_name], check=False)
    print(f"[Rhasspy Runner] Контейнер {container_name} остановлен.")


def status_rhasspy(base_url: str = typer.Option(None), port: int = DEFAULT_PORT, container_name: str = DEFAULT_CONTAINER) -> dict:
    """Проверка статуса: docker и HTTP /api/health (если requests доступен)."""
    result = {"docker": None, "container": None, "http": None}

    has_docker = _docker_available()
    result["docker"] = has_docker

    if has_docker:
        ps = subprocess.run(["docker", "ps", "--format", "{{json .}}"], capture_output=True, text=True, check=False)
        running = []
        for line in ps.stdout.splitlines():
            try:
                obj = json.loads(line)
                running.append(obj)
            except Exception:
                pass
        result["container"] = any(obj.get("Names") == container_name for obj in running)

    url = base_url or f"http://127.0.0.1:{port}"
    if requests:
        try:
            r = requests.get(f"{url}/api/health", timeout=2)
            result["http"] = (r.status_code, r.text.strip())
        except Exception as e:
            result["http"] = str(e)
    else:
        result["http"] = "requests not installed"

    return result


def wait_until_ready(base_url: str = typer.Option(None), port: int = DEFAULT_PORT, timeout: int = 20) -> bool:
    if not requests:
        return False
    url = base_url or f"http://127.0.0.1:{port}"
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{url}/api/health", timeout=2)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False
