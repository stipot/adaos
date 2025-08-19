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


async def _import_all_handlers():
    # импортируем все установленные навыки → декораторы наполнят реестры
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

    # регистрация
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
        return

    await emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

    # heartbeat loop
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

    return asyncio.create_task(loop())


async def run_boot_sequence(app):
    conf = load_config()
    await emit("sys.boot.start", {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

    # 1) импортируем навыки
    await _import_all_handlers()

    # 2) регистрируем подписки
    await register_subscriptions()
    await emit("sys.bus.ready", {}, source="lifecycle", actor="system")

    # 3) режимы ролей
    if conf.role == "hub":
        # hub готов; контекст доступен локально
        await emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")
    else:
        task = await _member_register_and_heartbeat()
        if task:
            _BOOT_TASKS.append(task)

    # 4) узел готов
    await emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")


async def shutdown():
    # будущая остановка задач; сейчас просто отменяем heartbeat
    for t in _BOOT_TASKS:
        t.cancel()
