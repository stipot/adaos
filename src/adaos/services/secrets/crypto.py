from __future__ import annotations
import base64, os
from cryptography.fernet import Fernet

ENV_MASTER = "ADAOS_VAULT_MASTER_KEY"


def generate_key() -> bytes:
    return Fernet.generate_key()


def load_or_create_master(getter, setter) -> bytes:
    """
    getter() -> bytes|None, setter(bytes) -> None
    1) читаем ключ из getter (напр., OS keyring)
    2) если нет — берём из ENV или генерируем и сохраняем через setter
    """
    k = getter()
    if k:
        return k
    env = os.getenv(ENV_MASTER, "").strip()
    if env:
        try:
            return base64.urlsafe_b64decode(env.encode("utf-8"))
        except Exception:
            pass
    k = generate_key()
    setter(k)
    return k


def fernet_from_key(k: bytes) -> Fernet:
    return Fernet(k)
