from __future__ import annotations
import asyncio
import socket
import time
import uuid
from typing import List, Optional, Sequence, Any

from adaos.services.agent_context import AgentContext
from adaos.sdk import bus
from adaos.agent.core.node_config import load_config, set_role as cfg_set_role, NodeConfig
from adaos.ports.heartbeat import HeartbeatPort
from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.ports.subnet import SubnetRegistryPort


class BootstrapService:
    def __init__(
        self,
        ctx: AgentContext,
        *,
        heartbeat: HeartbeatPort,
        skills_loader: SkillsLoaderPort,
        subnet_registry: SubnetRegistryPort,
    ) -> None:
        self.ctx = ctx
        self.heartbeat = heartbeat
        self.skills_loader = skills_loader
        self.subnet_registry = subnet_registry

        self._boot_tasks: List[asyncio.Task] = []
        self._ready_event: asyncio.Event = asyncio.Event()
        self._booted: bool = False
        self._app: Any = None  # FastAPI | None

    @classmethod
    def from_ctx(
        cls,
        ctx: AgentContext,
        *,
        heartbeat: HeartbeatPort,
        skills_loader: SkillsLoaderPort,
        subnet_registry: SubnetRegistryPort,
    ) -> "BootstrapService":
        return cls(ctx, heartbeat=heartbeat, skills_loader=skills_loader, subnet_registry=subnet_registry)

    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    async def _member_register_and_heartbeat(self, conf: NodeConfig) -> Optional[asyncio.Task]:
        assert conf.role == "member"
        ok = await self.heartbeat.register(
            conf.hub_url or "",
            conf.token or "",
            node_id=conf.node_id,
            subnet_id=conf.subnet_id,
            hostname=socket.gethostname(),
            roles=["member"],
        )
        if not ok:
            await bus.emit(
                "net.subnet.register.error",
                {"status": "non-200 or exception"},
                source="lifecycle",
                actor="system",
            )
            return None

        await bus.emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

        async def loop() -> None:
            backoff = 1
            while True:
                try:
                    ok_hb = await self.heartbeat.heartbeat(conf.hub_url or "", conf.token or "", node_id=conf.node_id)
                    if ok_hb:
                        backoff = 1
                    else:
                        await bus.emit(
                            "net.subnet.heartbeat.warn",
                            {"status": "non-200"},
                            source="lifecycle",
                            actor="system",
                        )
                        backoff = min(backoff * 2, 30)
                except Exception as e:
                    await bus.emit(
                        "net.subnet.heartbeat.error",
                        {"error": str(e)},
                        source="lifecycle",
                        actor="system",
                    )
                    backoff = min(backoff * 2, 30)
                await asyncio.sleep(backoff if backoff > 1 else 5)

        return asyncio.create_task(loop(), name="adaos-heartbeat")

    async def run_boot_sequence(self, app: Any) -> None:
        if self._booted:
            return
        self._app = app

        conf = load_config(ctx=self.ctx)
        await bus.emit(
            "sys.boot.start",
            {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id},
            source="lifecycle",
            actor="system",
        )

        # загрузка хендлеров и подписок
        await self.skills_loader.import_all_handlers(self.ctx.paths.skills_dir())
        from adaos.sdk.decorators import register_subscriptions  # локальный импорт, без ранних побочек

        await register_subscriptions()
        await bus.emit("sys.bus.ready", {}, source="lifecycle", actor="system")

        if conf.role == "hub":
            await bus.emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

            async def lease_monitor() -> None:
                while True:
                    down_list = self.subnet_registry.mark_down_if_expired()
                    for info in down_list:
                        await bus.emit("net.subnet.node.down", {"node_id": getattr(info, "node_id", None)}, source="lifecycle", actor="system")
                    await asyncio.sleep(5)

            self._boot_tasks.append(asyncio.create_task(lease_monitor(), name="adaos-lease-monitor"))
            self._ready_event.set()
            self._booted = True
            await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")
        else:
            task = await self._member_register_and_heartbeat(conf)
            if task:
                self._boot_tasks.append(task)
                self._ready_event.set()
                self._booted = True
                await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")

    async def shutdown(self) -> None:
        await bus.emit("sys.stopping", {}, source="lifecycle", actor="system")
        for t in list(self._boot_tasks):
            try:
                t.cancel()
            except Exception:
                pass
        if self._boot_tasks:
            await asyncio.gather(*self._boot_tasks, return_exceptions=True)
            self._boot_tasks.clear()
        self._booted = False
        self._ready_event.clear()
        await bus.emit("sys.stopped", {}, source="lifecycle", actor="system")

    async def switch_role(self, app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
        prev = load_config(ctx=self.ctx)
        await self.shutdown()

        # member → hub: корректная дерегистрация и новая подсеть
        if prev.role == "member" and role.lower().strip() == "hub" and prev.hub_url:
            try:
                await self.heartbeat.deregister(prev.hub_url, prev.token or "", node_id=prev.node_id)
            except Exception:
                pass
            subnet_id = subnet_id or str(uuid.uuid4())

        conf = cfg_set_role(role, hub_url=hub_url, subnet_id=subnet_id, ctx=self.ctx)
        await self.run_boot_sequence(app or self._app)
        return conf
