# \src\adaos\services\secrets\service.py
from __future__ import annotations
from typing import Iterable, Dict, Any
from adaos.ports.secrets import Secrets, SecretScope
from adaos.ports import Capabilities


class SecretsService:
    def __init__(self, backend: Secrets, caps: Capabilities):
        self.backend = backend
        self.caps = caps

    def put(self, key: str, value: str, *, scope: SecretScope = "profile", meta: dict | None = None) -> None:
        self.caps.require("core", "secrets.write")
        self.backend.put(key, value, scope=scope, meta=meta)

    def get(self, key: str, *, default: str | None = None, scope: SecretScope = "profile") -> str | None:
        self.caps.require("core", "secrets.read")
        return self.backend.get(key, default=default, scope=scope)

    def delete(self, key: str, *, scope: SecretScope = "profile") -> None:
        self.caps.require("core", "secrets.write")
        self.backend.delete(key, scope=scope)

    def list(self, *, scope: SecretScope = "profile") -> list[dict]:
        self.caps.require("core", "secrets.read")
        return self.backend.list(scope=scope)

    def import_items(self, items: Iterable[Dict[str, Any]], *, scope: SecretScope = "profile") -> int:
        self.caps.require("core", "secrets.write")
        return self.backend.import_items(items, scope=scope)

    def export_items(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        self.caps.require("core", "secrets.read")
        return self.backend.export_items(scope=scope)
