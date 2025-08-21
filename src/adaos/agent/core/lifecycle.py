# src\adaos\agent\core\lifecycle.py
from __future__ import annotations
import asyncio, socket, time, importlib.util
from pathlib import Path
from typing import List
import uuid

import requests

from adaos.agent.core.node_config import load_config, set_role as cfg_set_role, NodeConfig
from adaos.sdk.context import SKILLS_DIR
from adaos.sdk.decorators import register_subscriptions
import adaos.sdk.bus as bus
from adaos.agent.core.subnet_context import CTX
from adaos.agent.core.subnet_registry import mark_down_if_expired, get_node, NodeInfo

_BOOT_TASKS: List[asyncio.Task] = []
_BOOTED: bool = False
_READY_EVENT: asyncio.Event | None = None
_APP = None  # текущий FastAPI app


def is_ready() -> bool:
    ev = _READY_EVENT
    return bool(ev and ev.is_set())


async def _import_all_handlers():
    root = Path(SKILLS_DIR)
    for handler in root.rglob("handlers/main.py"):
        spec = importlib.util.spec_from_file_location("handler", handler)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


async def _member_register_and_heartbeat(conf: NodeConfig):
    assert conf.role == "member"
    url_reg = f"{conf.hub_url.rstrip('/')}/api/subnet/register"
    url_hb = f"{conf.hub_url.rstrip('/')}/api/subnet/heartbeat"
    headers = {"X-AdaOS-Token": conf.token}

    payload = {
        "node_id": conf.node_id,
        "subnet_id": conf.subnet_id,
        "hostname": socket.gethostname(),
        "roles": ["member"],
    }
    # первая попытка регистрации — синхронно, чтобы понимать готовность
    try:
        r = requests.post(url_reg, json=payload, headers=headers, timeout=3)
        if r.status_code != 200:
            await bus.emit("net.subnet.register.error", {"status": r.status_code, "text": r.text}, source="lifecycle", actor="system")
            return None
    except Exception as e:
        await bus.emit("net.subnet.register.error", {"error": str(e)}, source="lifecycle", actor="system")
        return None

    await bus.emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

    async def loop():
        backoff = 1
        while True:
            try:
                rr = requests.post(url_hb, json={"node_id": conf.node_id}, headers=headers, timeout=3)
                if rr.status_code == 200:
                    backoff = 1
                else:
                    await bus.emit("net.subnet.heartbeat.warn", {"status": rr.status_code, "text": rr.text}, source="lifecycle", actor="system")
                    backoff = min(backoff * 2, 30)
            except Exception:
                await bus.emit("net.subnet.heartbeat.error", {"error": str(e)}, source="lifecycle", actor="system")
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(backoff if backoff > 1 else 5)

    return asyncio.create_task(loop(), name="adaos-heartbeat")


async def run_boot_sequence(app):
    global _BOOTED, _READY_EVENT, _APP
    if _BOOTED:
        return
    _APP = app
    _READY_EVENT = _READY_EVENT or asyncio.Event()

    conf = load_config()
    await bus.emit("sys.boot.start", {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

    await _import_all_handlers()
    await register_subscriptions()
    await bus.emit("sys.bus.ready", {}, source="lifecycle", actor="system")

    if conf.role == "hub":
        await bus.emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

        # стартуем lease‑монитор нод
        async def lease_monitor():
            while True:
                # отмечаем down просроченные ноды и шлём события
                down_list = mark_down_if_expired()
                for info in down_list:
                    await bus.emit("net.subnet.node.down", {"node_id": info.node_id}, source="lifecycle", actor="system")
                await asyncio.sleep(5)

        _BOOT_TASKS.append(asyncio.create_task(lease_monitor(), name="adaos-lease-monitor"))
        # hub готов сразу
        _READY_EVENT.set()
        _BOOTED = True
        await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")
    else:
        # для member флаг ready поднимаем ТОЛЬКО после успешной регистрации
        task = await _member_register_and_heartbeat(conf)
        if task:
            _BOOT_TASKS.append(task)
            _READY_EVENT.set()
            _BOOTED = True
            await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")


async def shutdown():
    # сигнал остановки
    await bus.emit("sys.stopping", {}, source="lifecycle", actor="system")
    # отменяем фоновые задачи (heartbeat и пр.)
    for t in list(_BOOT_TASKS):
        try:
            t.cancel()
        except Exception:
            pass
    if _BOOT_TASKS:
        await asyncio.gather(*_BOOT_TASKS, return_exceptions=True)
        _BOOT_TASKS.clear()
    # сбрасываем флаги готовности/бута, чтобы можно было перезапустить boot
    global _BOOTED, _READY_EVENT
    _BOOTED = False
    if _READY_EVENT:
        try:
            _READY_EVENT.clear()
        except Exception:
            pass
    await bus.emit("sys.stopped", {}, source="lifecycle", actor="system")


async def switch_role(app, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
    """
    Переключение роли узла «на лету»:
      1) корректно останавливаем фоновые задачи,
      2) сохраняем новую роль в node.yaml,
      3) повторно выполняем boot‑последовательность.
    """
    # текущая конфигурация до смены
    prev = load_config()
    await shutdown()

    # если переходим из member → hub, корректно дерегистрируемся у старого hub
    if prev.role == "member" and role.lower().strip() == "hub" and prev.hub_url:
        try:
            requests.post(
                prev.hub_url.rstrip("/") + "/api/subnet/deregister",
                json={"node_id": prev.node_id},
                headers={"X-AdaOS-Token": prev.token},
                timeout=3,
            )
        except Exception:
            # без фейла — если хаб недоступен, просто продолжаем
            pass
        # создаём НОВУЮ подсеть для этого хаба
        subnet_id = subnet_id or str(uuid.uuid4())

    conf = cfg_set_role(role, hub_url=hub_url, subnet_id=subnet_id)
    await run_boot_sequence(app or _APP)
    return conf
