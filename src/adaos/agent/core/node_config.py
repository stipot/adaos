from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import os, uuid, yaml

from adaos.sdk.context import get_base_dir

CONFIG_PATH = Path(f"{get_base_dir()}/node.yaml")


def _config_path() -> Path:
    """Всегда вычисляем путь динамически из общего контекста."""
    base = Path(get_base_dir()).expanduser().resolve()
    p = base / "node.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class NodeConfig:
    node_id: str
    subnet_id: str
    role: str  # "hub" | "member"
    hub_url: str | None = None
    token: str | None = None  # pre-shared для локальной подсети (пока)


def _default_conf() -> NodeConfig:
    return NodeConfig(
        node_id=str(uuid.uuid4()),
        subnet_id=str(uuid.uuid4()),
        role="hub",
        hub_url=None,
        token=os.environ.get("ADAOS_TOKEN", "dev-local-token"),
    )


def load_config() -> NodeConfig:
    path = _config_path()
    if not path.exists():
        conf = _default_conf()
        save_config(conf)
        return conf
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # заполняем недостающие поля
    node_id = data.get("node_id") or str(uuid.uuid4())
    subnet_id = data.get("subnet_id") or str(uuid.uuid4())
    role = (data.get("role") or "hub").strip().lower()
    hub_url = data.get("hub_url")
    token = data.get("token") or os.environ.get("ADAOS_TOKEN", "dev-local-token")
    return NodeConfig(node_id=node_id, subnet_id=subnet_id, role=role, hub_url=hub_url, token=token)


def save_config(conf: NodeConfig) -> None:
    path = _config_path()
    path.write_text(yaml.safe_dump(asdict(conf), allow_unicode=True), encoding="utf-8")


def set_role(role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
    role = role.lower().strip()
    if role not in ("hub", "member"):
        raise ValueError("role must be 'hub' or 'member'")
    conf = load_config()
    conf.role = role
    if subnet_id:
        conf.subnet_id = subnet_id
    conf.hub_url = hub_url if role == "member" else None
    save_config(conf)
    return conf
