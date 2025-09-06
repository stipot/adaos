from __future__ import annotations
import json
from pathlib import Path
from typing import Optional, Dict, Any, Iterable
from cryptography.fernet import InvalidToken
from adaos.services.secrets.crypto import load_or_create_master, fernet_from_key
from adaos.ports.secrets import Secrets, SecretScope
from adaos.services.fs.safe_io import ensure_dir, read_text, write_text_atomic
from adaos.ports.fs import FSPolicy


class FileVault(Secrets):
    """
    Фолбэк-хранилище: {base_dir}/state/vault.json (AES/Fernet).
    Мастер-ключ хранится в OS keyring (или берётся из ENV для CI).
    """

    def __init__(self, *, base_dir: str, fs: FSPolicy, key_get, key_set):
        self.fs = fs
        self.base = Path(base_dir)
        self.vault_path = self.base / "state" / "vault.json"
        self.master = load_or_create_master(key_get, key_set)
        self.fernet = fernet_from_key(self.master)

    def _load(self) -> Dict[str, Any]:
        if not self.vault_path.exists():
            return {"profile": {}, "global": {}}
        try:
            raw = read_text(str(self.vault_path), self.fs)
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"profile": {}, "global": {}}
        except Exception:
            return {"profile": {}, "global": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        ensure_dir(str(self.vault_path.parent), self.fs)
        write_text_atomic(str(self.vault_path), json.dumps(data, ensure_ascii=False, indent=2), self.fs)

    def _enc(self, plain: str) -> str:
        return self.fernet.encrypt(plain.encode("utf-8")).decode("utf-8")

    def _dec(self, token: str) -> str:
        try:
            return self.fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise PermissionError("vault decryption failed")

    def put(self, key: str, value: str, *, scope: SecretScope = "profile", meta: Optional[Dict[str, Any]] = None) -> None:
        data = self._load()
        bucket = data.setdefault(scope, {})
        bucket[key] = {"v": self._enc(value), "meta": meta or {}}
        self._save(data)

    def get(self, key: str, *, default: Optional[str] = None, scope: SecretScope = "profile") -> Optional[str]:
        data = self._load()
        bucket = data.get(scope, {})
        rec = bucket.get(key)
        if not rec:
            return default
        return self._dec(rec["v"])

    def delete(self, key: str, *, scope: SecretScope = "profile") -> None:
        data = self._load()
        bucket = data.get(scope, {})
        if key in bucket:
            bucket.pop(key)
            self._save(data)

    def list(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        data = self._load()
        bucket = data.get(scope, {})
        return [{"key": k, "meta": (bucket[k].get("meta") if isinstance(bucket[k], dict) else {})} for k in sorted(bucket.keys())]

    def import_items(self, items: Iterable[Dict[str, Any]], *, scope: SecretScope = "profile") -> int:
        cnt = 0
        data = self._load()
        bucket = data.setdefault(scope, {})
        for it in items:
            k = it.get("key")
            v = it.get("value")
            if not k or v is None:
                continue
            bucket[k] = {"v": self._enc(str(v)), "meta": it.get("meta") or {}}
            cnt += 1
        self._save(data)
        return cnt

    def export_items(self, *, scope: SecretScope = "profile") -> list[Dict[str, Any]]:
        data = self._load()
        bucket = data.get(scope, {})
        out = []
        for k, rec in bucket.items():
            try:
                out.append({"key": k, "value": self._dec(rec["v"]), "meta": rec.get("meta") or {}})
            except Exception:
                # если ключ был перевыпущен — скрываем
                out.append({"key": k, "value": None, "meta": rec.get("meta") or {}, "note": "unreadable"})
        return out
