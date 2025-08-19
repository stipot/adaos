from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import os, uuid, yaml

from adaos.sdk.context import get_base_dir

CONFIG_PATH = Path(f"{get_base_dir()}/node.yaml")


@dataclass
class NodeConfig:
    node_id: str
    subnet_id: str
    role: str  # "hub" | "member"
    hub_url: str | None = None
    token: str | None = None  # pre-shared для локальной подсети (пока)


def load_config() -> NodeConfig:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    node_id = data.get("node_id") or str(uuid.uuid4())
    subnet_id = data.get("subnet_id") or str(uuid.uuid4())
    role = data.get("role") or "hub"
    hub_url = data.get("hub_url")
    token = data.get("token") or os.environ.get("ADAOS_TOKEN", "dev-local-token")

    conf = NodeConfig(node_id=node_id, subnet_id=subnet_id, role=role, hub_url=hub_url, token=token)

    # создаём дефолтный конфиг, если не было
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(yaml.safe_dump(asdict(conf), allow_unicode=True), encoding="utf-8")

    return conf
