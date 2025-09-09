from __future__ import annotations
import os, sys, json, hashlib, shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Sequence, Optional


@dataclass
class DevSpec:
    # чем «сигнатурим» набор зависимостей: pyproject+lock или requirements-dev.txt
    repo_root: Path
    pyproject: Optional[str]
    requirements_dev: Optional[str]

    def hash(self) -> str:
        h = hashlib.sha256()
        h.update(str(self.repo_root).encode())
        if self.pyproject is not None:
            h.update(self.pyproject.encode())
        if self.requirements_dev is not None:
            h.update(self.requirements_dev.encode())
        return h.hexdigest()[:16]


def _venv_paths(base_dir: Path):
    venv = base_dir / ".sandbox" / "venv"
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    py = bin_dir / ("python.exe" if os.name == "nt" else "python")
    pip = [str(py), "-m", "pip"]
    return venv, py, pip


def _read(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.exists() else None


def detect_spec(repo_root: Path) -> DevSpec:
    return DevSpec(
        repo_root=repo_root,
        pyproject=_read(repo_root / "pyproject.toml"),
        requirements_dev=_read(repo_root / "requirements-dev.txt"),
    )


def ensure_dev_venv(*, base_dir: Path, repo_root: Path, run: callable, env: dict | None = None) -> Path:
    """
    - base_dir: ADAOS_BASE_DIR (cwd песочницы)
    - repo_root: корень текущего репо (где pyproject.toml)
    - run(cmd: Sequence[str], cwd: str|Path, env: dict|None) -> (exit:int, out:str, err:str)
      любая функция, запускающая команду в нашей песочнице (обёртка над SandboxRunner).
    Возвращает путь к python из подготовленного venv.
    """
    venv, py, pip = _venv_paths(base_dir)
    marker = venv / ".adaos-dev.marker"

    spec = detect_spec(repo_root)
    sig = spec.hash()
    need_create = not venv.exists() or not (marker.exists() and marker.read_text(encoding="utf-8").strip() == sig)

    if need_create:
        # чисто пересоздадим venv
        if venv.exists():
            shutil.rmtree(venv, ignore_errors=True)

        # 1) python -m venv
        code, out, err = run([sys.executable, "-m", "venv", str(venv)], cwd=base_dir, env=env)
        if code != 0:
            raise RuntimeError(f"venv create failed: {err or out}")

        # 2) pip bootstrap
        code, out, err = run([*pip, "install", "-U", "pip", "setuptools", "wheel"], cwd=base_dir, env=env)
        if code != 0:
            raise RuntimeError(f"pip bootstrap failed: {err or out}")

        # 3) dev deps: предпочтём локальный проект с extras [dev], иначе requirements-dev.txt
        if spec.pyproject and ("[project.optional-dependencies]" in spec.pyproject or "[tool.poetry.group.dev]" in spec.pyproject):
            code, out, err = run([*pip, "install", "-e", f"{repo_root}[dev]"], cwd=base_dir, env=env)
        elif spec.requirements_dev:
            # сохраним во временный файл внутри base_dir, чтобы не ходить вне cwd
            req = base_dir / ".sandbox" / "requirements-dev.txt"
            req.parent.mkdir(parents=True, exist_ok=True)
            req.write_text(spec.requirements_dev, encoding="utf-8")
            code, out, err = run([*pip, "install", "-r", str(req)], cwd=base_dir, env=env)
        else:
            # минимально нужное
            code, out, err = run([*pip, "install", "pytest", "pytest-asyncio", "rich", "typer"], cwd=base_dir, env=env)
        if code != 0:
            raise RuntimeError(f"dev deps install failed: {err or out}")

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(sig, encoding="utf-8")

    return py
