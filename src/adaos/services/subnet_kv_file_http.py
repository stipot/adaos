from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Optional
import json, os, requests
from adaos.ports.subnet_kv import SubnetKVPort
from adaos.services.node_config import load_config


def _default_base_dir() -> Path:
    env = os.environ.get("ADAOS_BASE_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "AdaOS"
    return Path.home() / ".adaos"


def _resolve_base_dir() -> Path:
    try:
        from adaos.services.agent_context import get_ctx  # type: ignore

        try:
            return Path(get_ctx().paths.base_dir())  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception:
        pass
    return _default_base_dir()


class HubSubnetKV(SubnetKVPort):
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._path = (base_dir or _resolve_base_dir()) / "subnet_ctx.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._mem: Dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._mem = {}

    def is_hub(self) -> bool:
        return True

    def get(self, key: str, default: Any = None) -> Any:
        return self._mem.get(key, default)

    def set(self, key: str, value: Any) -> bool:
        self._mem[key] = value
        try:
            self._path.write_text(json.dumps(self._mem, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception:
            return False


class MemberSubnetKV(SubnetKVPort):
    def __init__(self) -> None:
        self._conf = load_config()

    def is_hub(self) -> bool:
        return False

    def get(self, key: str, default: Any = None) -> Any:
        url = f"{self._conf.hub_url.rstrip('/')}/api/subnet/context/{key}"
        r = requests.get(url, headers={"X-AdaOS-Token": self._conf.token}, timeout=3)
        if r.status_code == 200:
            try:
                return r.json().get("value", default)
            except Exception:
                return default
        return default

    def set(self, key: str, value: Any) -> bool:
        url = f"{self._conf.hub_url.rstrip('/')}/api/subnet/context/{key}"
        r = requests.put(url, json={"value": value}, headers={"X-AdaOS-Token": self._conf.token}, timeout=3)
        return r.status_code == 200


# factory/singleton
_SVC: SubnetKVPort | None = None


def get_subnet_kv() -> SubnetKVPort:
    global _SVC
    if _SVC is None:
        conf = load_config()
        _SVC = HubSubnetKV() if (conf.role or "hub") == "hub" else MemberSubnetKV()
    return _SVC
