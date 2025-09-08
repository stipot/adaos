from __future__ import annotations
import json, os, shutil, tempfile
from pathlib import Path
from typing import Any
from adaos.ports.fs import FSPolicy


def ensure_dir(path: str, fs: FSPolicy) -> str:
    fs.require_write(path)
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def write_text_atomic(path: str, data: str, fs: FSPolicy) -> None:
    fs.require_write(path)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, p)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_json_atomic(path: str, obj: Any, fs: FSPolicy) -> None:
    write_text_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2), fs)


def read_text(path: str, fs: FSPolicy) -> str:
    fs.require_read(path)
    return Path(path).read_text(encoding="utf-8")


def remove_tree(path: str, fs: FSPolicy) -> None:
    fs.require_write(path)
    p = Path(path)
    if p.exists():
        shutil.rmtree(p)
