from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
import hashlib, base64, os

from adaos.sdk.context import get_ctx
from adaos.ports.root import PubKey, KeypairRef


@dataclass
class KeyStore:
    base: Path
    priv_path: Path
    pub_path: Path


def _base_dir() -> Path:
    # все артефакты root держим здесь
    return Path(get_ctx().paths.base()) / "root"


def _fp(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_ed25519(hub_id: str) -> tuple[KeypairRef, KeyStore]:
    """создаёт пару ключей, если их ещё нет; возвращает ссылку и пути."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    base = _base_dir()
    base.mkdir(parents=True, exist_ok=True)
    priv_p = base / f"{hub_id}.ed25519.key"
    pub_p = base / f"{hub_id}.ed25519.pub"

    if not priv_p.exists() or not pub_p.exists():
        priv = ed25519.Ed25519PrivateKey.generate()
        pub = priv.public_key()
        priv_p.write_bytes(
            priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        pub_p.write_bytes(
            pub.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    pub_pem = pub_p.read_bytes()
    ref = KeypairRef(
        hub_id=hub_id,
        pub=PubKey(alg="ed25519", pem=pub_pem.decode(), fingerprint=_fp(pub_pem)),
        created_at=datetime.utcnow(),
    )
    return ref, KeyStore(base=base, priv_path=priv_p, pub_path=pub_p)


def sign_nonce_ed25519(hub_id: str, nonce_b64: str) -> str:
    """подписывает nonce приватным ключом hub-а; возвращает подпись base64url."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    priv_p = _base_dir() / f"{hub_id}.ed25519.key"
    priv = serialization.load_pem_private_key(priv_p.read_bytes(), password=None)
    sig = priv.sign(base64.urlsafe_b64decode(nonce_b64))
    return base64.urlsafe_b64encode(sig).decode()


def save_cert_bundle(hub_id: str, cert_pem: str, ca_pem: str) -> tuple[Path, Path]:
    base = _base_dir()
    cert_dir = base / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / f"{hub_id}.crt.pem"
    ca_path = cert_dir / "root-ca.pem"
    cert_path.write_text(cert_pem, encoding="utf-8")
    ca_path.write_text(ca_pem, encoding="utf-8")
    return cert_path, ca_path
