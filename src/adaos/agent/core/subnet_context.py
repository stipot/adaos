from __future__ import annotations
from pathlib import Path
from typing import Any, Dict
import json, time, requests

from adaos.agent.core.node_config import load_config

from adaos.sdk.context import get_base_dir

_CTX_PATH = Path(f"{get_base_dir()}/subnet_ctx.json")


class SubnetContext:
    """
    Контекст подсети (KV). На hub — локальный файл + память.
    На member — HTTP прокси к hub.
    """

    def __init__(self):
        self.conf = load_config()
        self._mem: Dict[str, Any] = {}
        if self.is_hub():
            _CTX_PATH.parent.mkdir(parents=True, exist_ok=True)
            if _CTX_PATH.exists():
                try:
                    self._mem = json.loads(_CTX_PATH.read_text(encoding="utf-8")) or {}
                except Exception:
                    self._mem = {}

    def is_hub(self) -> bool:
        return (self.conf.role or "hub") == "hub"

    # HUB MODE
    def hub_get(self, key: str, default: Any = None) -> Any:
        return self._mem.get(key, default)

    def hub_set(self, key: str, value: Any) -> None:
        self._mem[key] = value
        try:
            _CTX_PATH.write_text(json.dumps(self._mem, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # MEMBER MODE (proxy)
    def member_get(self, key: str, default: Any = None) -> Any:
        url = f"{self.conf.hub_url.rstrip('/')}/api/subnet/context/{key}"
        r = requests.get(url, headers={"X-AdaOS-Token": self.conf.token}, timeout=3)
        if r.status_code == 200:
            return r.json().get("value", default)
        return default

    def member_set(self, key: str, value: Any) -> bool:
        url = f"{self.conf.hub_url.rstrip('/')}/api/subnet/context/{key}"
        r = requests.put(url, json={"value": value}, headers={"X-AdaOS-Token": self.conf.token}, timeout=3)
        return r.status_code == 200

    # Unified API
    def get(self, key: str, default: Any = None) -> Any:
        return self.hub_get(key, default) if self.is_hub() else self.member_get(key, default)

    def set(self, key: str, value: Any) -> bool:
        if self.is_hub():
            self.hub_set(key, value)
            return True
        return self.member_set(key, value)


CTX = SubnetContext()
