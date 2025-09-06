from __future__ import annotations
from pathlib import Path


class SimpleFSPolicy:
    """
    Разрешает доступ только внутри заранее заданных корней.
    Проверка по realpath/resolve(), защищает от traversal и symlink'ов.
    """

    def __init__(self) -> None:
        self._roots: list[Path] = []

    def allow_root(self, root: str) -> None:
        p = Path(root).resolve()
        if p not in self._roots:
            self._roots.append(p)

    def _check(self, path: str) -> None:
        p = Path(path).resolve()
        for r in self._roots:
            try:
                p.relative_to(r)
                return
            except ValueError:
                continue
        raise PermissionError(f"fs policy: path not allowed: {p}")

    def require_read(self, path: str) -> None:
        self._check(path)

    def require_write(self, path: str) -> None:
        self._check(path)

    def is_allowed(self, path: str) -> bool:
        try:
            self._check(path)
            return True
        except PermissionError:
            return False
