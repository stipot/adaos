from __future__ import annotations
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.bootstrap import BootstrapService
from adaos.services.heartbeat_requests import RequestsHeartbeat
from adaos.services.skills_loader_importlib import ImportlibSkillsLoader
from adaos.services.subnet_registry_adapter import SubnetRegistryAdapter
from adaos.agent.core.node_config import NodeConfig

_SERVICE: BootstrapService | None = None


def _svc() -> BootstrapService:
    global _SERVICE
    if _SERVICE is None:
        ctx = get_ctx()
        _SERVICE = BootstrapService.from_ctx(
            ctx,
            heartbeat=RequestsHeartbeat(),
            skills_loader=ImportlibSkillsLoader(),
            subnet_registry=SubnetRegistryAdapter(),
        )
    return _SERVICE


def is_ready() -> bool:
    return _svc().is_ready()


async def run_boot_sequence(app: Any) -> None:
    await _svc().run_boot_sequence(app)


async def shutdown() -> None:
    await _svc().shutdown()


async def switch_role(app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
    return await _svc().switch_role(app, role, hub_url=hub_url, subnet_id=subnet_id)
