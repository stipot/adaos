# src\adaos\services\bootstrap.py
from __future__ import annotations
import asyncio, socket, time, uuid, os
from typing import Any, List, Optional, Sequence
from pathlib import Path
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.sdk import bus
from adaos.services.node_config import load_config, set_role as cfg_set_role, NodeConfig
from adaos.ports.heartbeat import HeartbeatPort
from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.ports.subnet_registry import SubnetRegistryPort
from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.adapters.scenarios.git_repo import GitScenarioRepository


class BootstrapService:
    def __init__(self, ctx: AgentContext, *, heartbeat: HeartbeatPort, skills_loader: SkillsLoaderPort, subnet_registry: SubnetRegistryPort) -> None:
        self.ctx = ctx
        self.heartbeat = heartbeat
        self.skills_loader = skills_loader
        self.subnet_registry = subnet_registry
        self._boot_tasks: List[asyncio.Task] = []
        self._ready = asyncio.Event()
        self._booted = False
        self._app: Any = None

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def _prepare_environment(self) -> None:
        """
        Гарантированная подготовка окружения:
          - создаёт каталоги (skills, scenarios, state, cache, logs)
          - инициализирует схему БД (skills/scenarios)
          - при наличии URL монорепо — клонирует репозитории без установки
        """
        ctx = self.ctx

        # каталоги (учитываем, что в paths могут быть callables)
        def _resolve(x):
            return x() if callable(x) else x

        skills_root = Path(_resolve(getattr(ctx.paths, "skills_dir", "")))
        scenarios_root = Path(_resolve(getattr(ctx.paths, "scenarios_dir", "")))
        state_root = Path(_resolve(getattr(ctx.paths, "state_dir", "")))
        cache_root = Path(_resolve(getattr(ctx.paths, "cache_dir", "")))
        logs_root = Path(_resolve(getattr(ctx.paths, "logs_dir", "")))

        for p in (skills_root, scenarios_root, state_root, cache_root, logs_root):
            if p:
                p.mkdir(parents=True, exist_ok=True)

        # схема БД (единая функция, не через побочный эффект конкретного реестра)
        ensure_schema(ctx.sql)

        # в тестах — не трогаем удалённые репозитории/сеть
        if os.getenv("ADAOS_TESTING") == "1":
            return

        # монорепо навыков
        try:
            if ctx.settings.skills_monorepo_url and not (skills_root / ".git").exists():
                GitSkillRepository(
                    paths=ctx.paths,
                    git=ctx.git,
                    monorepo_url=getattr(ctx.settings, "skills_monorepo_url", None),
                    monorepo_branch=getattr(ctx.settings, "skills_monorepo_branch", None),
                ).ensure()
        except Exception:
            # не блокируем бут при сбое ensure; логирование можно добавить позже
            pass

        # монорепо сценариев (поддержим оба возможных конструктора)
        try:
            if ctx.settings.scenarios_monorepo_url and not (scenarios_root / ".git").exists():
                try:
                    # вариант с именами url/branch
                    GitScenarioRepository(
                        paths=ctx.paths,
                        git=ctx.git,
                        url=getattr(ctx.settings, "scenarios_monorepo_url", None),
                        branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
                    ).ensure()
                except TypeError:
                    # вариант с именами monorepo_url/monorepo_branch
                    GitScenarioRepository(
                        paths=ctx.paths,
                        git=ctx.git,
                        monorepo_url=getattr(ctx.settings, "scenarios_monorepo_url", None),
                        monorepo_branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
                    ).ensure()
        except Exception:
            pass

    async def _member_register_and_heartbeat(self, conf: NodeConfig) -> Optional[asyncio.Task]:
        ok = await self.heartbeat.register(conf.hub_url or "", conf.token or "", node_id=conf.node_id, subnet_id=conf.subnet_id, hostname=socket.gethostname(), roles=["member"])
        if not ok:
            await bus.emit("net.subnet.register.error", {"status": "non-200"}, source="lifecycle", actor="system")
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
                        await bus.emit("net.subnet.heartbeat.warn", {"status": "non-200"}, source="lifecycle", actor="system")
                        backoff = min(backoff * 2, 30)
                except Exception as e:
                    await bus.emit("net.subnet.heartbeat.error", {"error": str(e)}, source="lifecycle", actor="system")
                    backoff = min(backoff * 2, 30)
                await asyncio.sleep(backoff if backoff > 1 else 5)

        return asyncio.create_task(loop(), name="adaos-heartbeat")

    async def run_boot_sequence(self, app: Any) -> None:
        if self._booted:
            return
        self._app = app
        conf = load_config(ctx=self.ctx)
        self._prepare_environment()
        await bus.emit("sys.boot.start", {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id}, source="lifecycle", actor="system")
        # paths.skills_dir может быть функцией; нормализуем до Path/str
        skills_dir_attr = getattr(self.ctx.paths, "skills_dir", None)
        skills_root = skills_dir_attr() if callable(skills_dir_attr) else skills_dir_attr
        await self.skills_loader.import_all_handlers(skills_root)
        from adaos.sdk.decorators import register_subscriptions

        await register_subscriptions()
        await bus.emit("sys.bus.ready", {}, source="lifecycle", actor="system")
        if conf.role == "hub":
            await bus.emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

            async def lease_monitor() -> None:
                while True:
                    for info in self.subnet_registry.mark_down_if_expired():
                        await bus.emit("net.subnet.node.down", {"node_id": getattr(info, "node_id", None)}, source="lifecycle", actor="system")
                    await asyncio.sleep(5)

            self._boot_tasks.append(asyncio.create_task(lease_monitor(), name="adaos-lease-monitor"))
            self._ready.set()
            self._booted = True
            await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")
        else:
            task = await self._member_register_and_heartbeat(conf)
            if task:
                self._boot_tasks.append(task)
                self._ready.set()
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
        self._ready.clear()
        await bus.emit("sys.stopped", {}, source="lifecycle", actor="system")

    async def switch_role(self, app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
        prev = load_config(ctx=self.ctx)
        await self.shutdown()
        if prev.role == "member" and role.lower().strip() == "hub" and prev.hub_url:
            try:
                await self.heartbeat.deregister(prev.hub_url, prev.token or "", node_id=prev.node_id)
            except Exception:
                pass
            subnet_id = subnet_id or str(uuid.uuid4())
        conf = cfg_set_role(role, hub_url=hub_url, subnet_id=subnet_id, ctx=self.ctx)
        await self.run_boot_sequence(app or self._app)
        return conf


# --- модульные фасады (синглтон) ---
from adaos.services.heartbeat_requests import RequestsHeartbeat
from adaos.services.skills_loader_importlib import ImportlibSkillsLoader
from adaos.services.subnet_registry_mem import get_subnet_registry

_SERVICE: BootstrapService | None = None


def _svc() -> BootstrapService:
    global _SERVICE
    if _SERVICE is None:
        ctx = get_ctx()
        _SERVICE = BootstrapService(ctx, heartbeat=RequestsHeartbeat(), skills_loader=ImportlibSkillsLoader(), subnet_registry=get_subnet_registry())
    return _SERVICE


def is_ready() -> bool:
    return _svc().is_ready()


async def run_boot_sequence(app: Any) -> None:
    await _svc().run_boot_sequence(app)


async def shutdown() -> None:
    await _svc().shutdown()


async def switch_role(app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
    return await _svc().switch_role(app, role, hub_url=hub_url, subnet_id=subnet_id)
