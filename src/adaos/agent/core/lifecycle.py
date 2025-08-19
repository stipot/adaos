from __future__ import annotations
import asyncio, socket, time, importlib.util
from pathlib import Path
from typing import List

import requests

from adaos.agent.core.node_config import load_config
from adaos.sdk.context import SKILLS_DIR
from adaos.sdk.decorators import register_subscriptions
from adaos.sdk.bus import emit
from adaos.agent.core.subnet_context import CTX

_BOOT_TASKS: List[asyncio.Task] = []
_BOOTED: bool = False
_READY_EVENT: asyncio.Event | None = None


def is_ready() -> bool:
    ev = _READY_EVENT
    return bool(ev and ev.is_set())


async def _import_all_handlers():
    root = Path(SKILLS_DIR)
    for handler in root.rglob("handlers/main.py"):
        spec = importlib.util.spec_from_file_location("handler", handler)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


async def _member_register_and_heartbeat():
    conf = load_config()
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
    try:
        r = requests.post(url_reg, json=payload, headers=headers, timeout=3)
        r.raise_for_status()
    except Exception as e:
        await emit("net.subnet.register.error", {"error": str(e)}, source="lifecycle", actor="system")
        return None

    await emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

    async def loop():
        backoff = 1
        while True:
            try:
                rr = requests.post(url_hb, json={"node_id": conf.node_id}, headers=headers, timeout=3)
                if rr.status_code == 200:
                    backoff = 1
                else:
                    backoff = min(backoff * 2, 30)
            except Exception:
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(backoff if backoff > 1 else 5)

    return asyncio.create_task(loop(), name="adaos-heartbeat")


async def run_boot_sequence(app):
    global _BOOTED, _READY_EVENT
    if _BOOTED:
        return
    _READY_EVENT = _READY_EVENT or asyncio.Event()

    conf = load_config()
    await emit("sys.boot.start", {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

    await _import_all_handlers()
    await register_subscriptions()
    await emit("sys.bus.ready", {}, source="lifecycle", actor="system")

    if conf.role == "hub":
        await emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")
    else:
        task = await _member_register_and_heartbeat()
        if task:
            _BOOT_TASKS.append(task)

    _READY_EVENT.set()
    _BOOTED = True
    await emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")


async def shutdown():
    # сигнал остановки
    await emit("sys.stopping", {}, source="lifecycle", actor="system")
    # отменяем фоновые задачи (heartbeat и пр.)
    for t in list(_BOOT_TASKS):
        try:
            t.cancel()
        except Exception:
            pass
    if _BOOT_TASKS:
        await asyncio.gather(*_BOOT_TASKS, return_exceptions=True)
        _BOOT_TASKS.clear()
    await emit("sys.stopped", {}, source="lifecycle", actor="system")
