from __future__ import annotations
import asyncio, os, re, time
from typing import Any, Dict, Optional
import httpx

from .dsl import Prototype
from .store import load_prototype, load_impl, apply_rewrite, load_bindings

ADAOS_BASE = os.environ.get("ADAOS_BASE", "http://127.0.0.1:8777")
ADAOS_TOKEN = os.environ.get("ADAOS_TOKEN", "dev-local-token")


def _subst(s: str, ctx: Dict[str, Any]) -> str:
    def get(path: str) -> Any:
        cur = ctx
        for p in path.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return ""
        return cur

    return re.sub(r"\$\{([^}]+)\}", lambda m: str(get(m.group(1))), s)


def _eval_if(expr: Optional[str], ctx: Dict[str, Any]) -> bool:
    if not expr:
        return True
    if expr.startswith("hasSlot("):
        name = expr[8:-1].strip().strip("'\"")
        return bool(ctx.get("slot", {}).get(name))
    if expr.startswith("has("):
        path = expr[4:-1].strip().strip("'\"")
        cur: Any = ctx
        for p in path.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return False
        return cur not in (None, "", 0, False)
    v = _subst(expr, ctx)
    return bool(v) and v not in ("false", "0", "None", "null")


class Instance:
    def __init__(self, iid: str, sid: str, user: str):
        self.iid, self.sid, self.user = iid, sid, user
        self.started = time.time()
        self.state = "running"
        self.activities: Dict[str, bool] = {}
        self._cancel = asyncio.Event()

    def cancel(self):
        self.state = "stopping"
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()


class InstanceManager:
    def __init__(self):
        self.tasks = {}
        self.instances: Dict[str, Instance] = {}

    def list(self):
        return [
            {"iid": iid, "sid": ins.sid, "user": ins.user, "state": ins.state, "started": ins.started, "activities": [k for k, v in ins.activities.items() if v]}
            for iid, ins in self.instances.items()
        ]

    def by_activity(self, activity: str):
        return [iid for iid, ins in self.instances.items() if ins.activities.get(activity)]


MANAGER = InstanceManager()


async def run_scenario(sid: str, user: str, io_override: Optional[Dict[str, Any]] = None) -> str:
    iid = f"{sid}:{int(time.time()*1000)}"
    inst = Instance(iid, sid, user)
    MANAGER.instances[iid] = inst

    async def _runner():
        async with httpx.AsyncClient(timeout=60.0) as cli:
            headers = {"X-AdaOS-Token": ADAOS_TOKEN, "Content-Type": "application/json"}
            try:
                proto = load_prototype(sid)
                imp = load_impl(sid, user)
                eff = apply_rewrite(proto, imp)
                if io_override:
                    eff.io.settings.update(io_override.get("settings", {}))
                ctx: Dict[str, Any] = {
                    "ctx": {"user": user},
                    "params": eff.params,
                    "slot": (load_bindings(sid, user).get("slots") or {}),  # минимальный биндинг: {"weather":{"skill":"open_weather"}}
                }
                aliases: Dict[str, Any] = {}
                last: Any = None

                for step in eff.steps:
                    if inst.is_cancelled():
                        break
                    if not _eval_if(step.if_, {**ctx, **aliases, "last": last}):
                        continue
                    if step.do == "say":
                        text = _subst(step.text or "", {**ctx, **aliases, "last": last})
                        r = await cli.post(f"{ADAOS_BASE}/api/say", headers=headers, json={"text": text})
                        r.raise_for_status()
                        last = await _maybe_json(r)
                    elif step.do == "call":
                        slot_name = step.slot or ""
                        target = ctx["slot"].get(slot_name)
                        if not target:
                            continue
                        args = {k: (_subst(v, {**ctx, **aliases, "last": last}) if isinstance(v, str) else v) for k, v in (step.args or {}).items()}
                        if step.activityId:
                            inst.activities[step.activityId] = True
                        r = await cli.post(f"{ADAOS_BASE}/api/skills/{target['skill']}/{step.method}", headers=headers, json=args)
                        r.raise_for_status()
                        if step.activityId:
                            inst.activities[step.activityId] = False
                        last = await _maybe_json(r)
                    elif step.do == "for":
                        arr = _eval_path(step.each or "", {**ctx, **aliases, "last": last})
                        if isinstance(arr, list):
                            for item in arr:
                                if inst.is_cancelled():
                                    break
                                _last_inner = None
                                for sub in step.steps or []:
                                    if not _eval_if(sub.if_, {**ctx, **aliases, "item": item, "last": _last_inner}):
                                        continue
                                    if sub.do == "say":
                                        text = _subst(sub.text or "", {**ctx, **aliases, "item": item, "last": _last_inner})
                                        r = await cli.post(f"{ADAOS_BASE}/api/say", headers=headers, json={"text": text})
                                        r.raise_for_status()
                                        _last_inner = await _maybe_json(r)
                                    elif sub.do == "call":
                                        slot_name = sub.slot or ""
                                        target = ctx["slot"].get(slot_name)
                                        if not target:
                                            continue
                                        sargs = {
                                            k: (_subst(v, {**ctx, **aliases, "item": item, "last": _last_inner}) if isinstance(v, str) else v) for k, v in (sub.args or {}).items()
                                        }
                                        r = await cli.post(f"{ADAOS_BASE}/api/skills/{target['skill']}/{sub.method}", headers=headers, json=sargs)
                                        r.raise_for_status()
                                        _last_inner = await _maybe_json(r)
                        last = None
                    elif step.do == "wait":
                        await _sleep_cancellable(step.sec or 0, inst)
                    elif step.do == "let":
                        ctx[step.var] = step.value
                    if step.alias:
                        aliases[step.alias] = last
                inst.state = "stopped" if not inst.is_cancelled() else "stopping"
            except asyncio.CancelledError:
                inst.state = "stopped"
            except Exception:
                inst.state = "failed"
            finally:
                inst.activities = {}

    MANAGER.tasks[iid] = asyncio.create_task(_runner())
    return iid


async def stop_instance(iid: str) -> bool:
    inst = MANAGER.instances.get(iid)
    if not inst:
        return False
    inst.cancel()
    task = MANAGER.tasks.get(iid)
    if task:
        task.cancel()
        try:
            await task
        except Exception:
            pass
    inst.state = "stopped"
    return True


async def stop_by_activity(activity: str):
    for iid in MANAGER.by_activity(activity):
        await stop_instance(iid)


async def _sleep_cancellable(sec: float, inst: Instance):
    end = time.time() + max(0.0, sec)
    while time.time() < end:
        if inst.is_cancelled():
            break
        await asyncio.sleep(0.1)


def _eval_path(expr: str, ctx: Dict[str, Any]):
    cur: Any = ctx
    for p in (expr or "").split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


async def _maybe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"ok": r.status_code in (200, 201)}
