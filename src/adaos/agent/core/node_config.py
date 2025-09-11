# src\adaos\agent\core\node_config.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import os, uuid, yaml, sys

# важно: не трогаем контекст при импорте модуля
try:
    from adaos.services.agent_context import get_ctx, AgentContext  # type: ignore
except Exception:  # модуль может импортироваться очень рано
    get_ctx = None  # type: ignore

    class AgentContext:  # type: ignore
        pass


def _default_base_dir() -> Path:
    """фолбэк, если контекст ещё не доступен"""
    env = os.environ.get("ADAOS_BASE_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "adaos"
    return Path.home() / ".adaos"


def _base_dir(ctx: AgentContext | None = None) -> Path:
    # 1) явный контекст
    if ctx and getattr(ctx, "paths", None):
        return Path(getattr(ctx.paths, "base"))
    # 2) пробуем глобальный get_ctx(), но не форсируем инициализацию
    if "adaos" in sys.modules and get_ctx:
        try:
            return Path(get_ctx().paths.base)  # type: ignore[attr-defined]
        except Exception:
            pass
    # 3) безопасный фолбэк
    return _default_base_dir()


def _config_path(ctx: AgentContext | None = None) -> Path:
    """всегда вычисляем путь динамически (через контекст, если он есть)"""
    p = _base_dir(ctx) / "node.yaml"
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


def load_config(ctx: AgentContext | None = None) -> NodeConfig:
    path = _config_path()
    if not path.exists():
        conf = _default_conf()
        save_config(conf, ctx=ctx)
        return conf
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # заполняем недостающие поля
    node_id = data.get("node_id") or str(uuid.uuid4())
    subnet_id = data.get("subnet_id") or str(uuid.uuid4())
    role = (data.get("role") or "hub").strip().lower()
    hub_url = data.get("hub_url")
    token = data.get("token") or os.environ.get("ADAOS_TOKEN", "dev-local-token")
    return NodeConfig(node_id=node_id, subnet_id=subnet_id, role=role, hub_url=hub_url, token=token)


def save_config(conf: NodeConfig, *, ctx: AgentContext | None = None) -> None:
    path = _config_path(ctx)
    path.write_text(yaml.safe_dump(asdict(conf), allow_unicode=True), encoding="utf-8")


def set_role(
    role: str,
    *,
    hub_url: str | None = None,
    subnet_id: str | None = None,
    ctx: AgentContext | None = None,
) -> NodeConfig:
    role = role.lower().strip()
    if role not in ("hub", "member"):
        raise ValueError("role must be 'hub' or 'member'")
    conf = load_config(ctx=ctx)
    conf.role = role
    if subnet_id:
        conf.subnet_id = subnet_id
    conf.hub_url = hub_url if role == "member" else None
    save_config(conf, ctx=ctx)
    return conf
