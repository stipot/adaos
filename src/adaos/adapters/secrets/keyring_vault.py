from __future__ import annotations
from typing import Optional, Dict, Any, Iterable
import json
import keyring
from time import time
from adaos.ports.secrets import Secrets, SecretScope
from adaos.ports import KV


class KeyringVault(Secrets):
    """
    Секреты храним в OS keyring под service='adaos:<scope>:<profile>'.
    Индекс ключей (для list/export) держим в KV ('secrets:index:<scope>').
    Поддерживает KV с API get/set ИЛИ get_json/set_json.
    """

    def __init__(self, *, profile: str, kv: KV):
        self.profile = profile
        self.kv = kv

    def _service(self, scope: SecretScope) -> str:
        return f"adaos:{scope}:{self.profile}"

    def _index_key(self, scope: SecretScope) -> str:
        return f"secrets:index:{scope}"

    # ---------- совместимость с разными KV ----------
    def _kv_get_json(self, key: str):
        # сначала пробуем "родной" JSON-метод
        try:
            return self.kv.get_json(key)
        except AttributeError:
            pass
        # затем строковый get + json.loads
        raw = None
        for meth in ("get", "read", "fetch"):
            if hasattr(self.kv, meth):
                raw = getattr(self.kv, meth)(key)
                break
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _kv_set_json(self, key: str, obj) -> None:
        # сначала пробуем "родной" JSON-метод
        try:
            self.kv.set_json(key, obj)
            return
        except AttributeError:
            pass
        data = json.dumps(obj, ensure_ascii=False)
        for meth in ("set", "write", "put"):
            if hasattr(self.kv, meth):
                getattr(self.kv, meth)(key, data)
                return
        raise AttributeError("KV has no suitable set/set_json method")

    def _load_index(self, scope: SecretScope) -> dict:
        return self._kv_get_json(self._index_key(scope)) or {}

    def _save_index(self, scope: SecretScope, idx: dict) -> None:
        self._kv_set_json(self._index_key(scope), idx)

    # -------------------------------------------------

    def put(self, key: str, value: str, *, scope: SecretScope = "profile", meta: Optional[Dict[str, Any]] = None) -> None:
        service = self._service(scope)
        keyring.set_password(service, key, value)
        idx = self._load_index(scope)
        idx[key] = {"meta": meta or {}, "updated_at": time()}
        self._save_index(scope, idx)

    def get(self, key: str, *, default: Optional[str] = None, scope: SecretScope = "profile") -> Optional[str]:
        v = keyring.get_password(self._service(scope), key)
        return v if v is not None else default

    def delete(self, key: str, *, scope: SecretScope = "profile") -> None:
        try:
            keyring.delete_password(self._service(scope), key)
        except keyring.errors.PasswordDeleteError:
            pass
        idx = self._load_index(scope)
        if key in idx:
            idx.pop(key)
            self._save_index(scope, idx)

    def list(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        idx = self._load_index(scope)
        return [{"key": k, "meta": v.get("meta", {})} for k, v in sorted(idx.items())]

    def import_items(self, items: Iterable[Dict[str, Any]], *, scope: SecretScope = "profile") -> int:
        cnt = 0
        idx = self._load_index(scope)
        for it in items:
            k = it.get("key")
            v = it.get("value")
            if not k or v is None:
                continue
            keyring.set_password(self._service(scope), k, str(v))
            idx[k] = {"meta": it.get("meta") or {}, "updated_at": time()}
            cnt += 1
        self._save_index(scope, idx)
        return cnt

    def export_items(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        out = []
        idx = self._load_index(scope)
        for k in sorted(idx.keys()):
            v = keyring.get_password(self._service(scope), k)
            out.append({"key": k, "value": v, "meta": idx[k].get("meta") or {}})
        return out
