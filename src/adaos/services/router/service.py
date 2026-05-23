from __future__ import annotations

from typing import Any, Callable
from pathlib import Path
import asyncio
import json
import time
import requests
import os
import shutil
import subprocess
import sys
import y_py as Y

from adaos.services.eventbus import LocalEventBus
import logging
from adaos.domain import Event
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from .rules_loader import load_rules, watch_rules
from .media_routes import resolve_media_route_intent
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.io_console import print_text
from adaos.services.subnet_alias import display_subnet_alias, load_subnet_alias
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter
from adaos.services.webspace_id import coerce_webspace_id
from adaos.services.yjs.doc import async_get_ydoc, async_read_ydoc, mutate_live_room
from adaos.services.yjs.store import ystore_write_metadata
from adaos.services.scenario.node_data_scope import node_scope_data_path
from adaos.services.scenario.projection_service import _merge_nested_path
from adaos.skills.runtime_runner import execute_tool
from adaos.sdk.io.context import io_meta


_log = logging.getLogger("adaos.router.service")


def _webio_stream_guard_enabled() -> bool:
    return str(os.getenv("ADAOS_WEBIO_STREAM_GUARD_ENABLE") or "1").strip().lower() in {"1", "true", "yes", "on"}


def _webio_stream_warn_bytes() -> int:
    try:
        return max(1024, int(str(os.getenv("ADAOS_WEBIO_STREAM_WARN_BYTES") or "65536").strip()))
    except Exception:
        return 65536


def _webio_stream_block_bytes() -> int:
    try:
        return max(_webio_stream_warn_bytes(), int(str(os.getenv("ADAOS_WEBIO_STREAM_BLOCK_BYTES") or "262144").strip()))
    except Exception:
        return max(_webio_stream_warn_bytes(), 262144)


def _webio_stream_payload_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return 0


def _webio_stream_owner(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    owner = str(
        payload.get("owner")
        or meta.get("owner")
        or payload.get("skill_owner")
        or meta.get("skill_owner")
        or ""
    ).strip()
    if owner:
        return owner
    skill_name = str(
        payload.get("skill_name")
        or meta.get("skill_name")
        or payload.get("skill")
        or meta.get("skill")
        or ""
    ).strip()
    return f"skill:{skill_name}" if skill_name else ""


def _webio_stream_admit(
    *,
    webspace_id: str,
    receiver: str,
    owner: str,
    payload_bytes: int,
    fanout_total: int = 1,
) -> bool:
    if not _webio_stream_guard_enabled():
        return True
    warn_bytes = _webio_stream_warn_bytes()
    block_bytes = _webio_stream_block_bytes()
    effective_bytes = max(0, int(payload_bytes or 0)) * max(1, int(fanout_total or 1))
    policy_state = "ok"
    reason = "healthy"
    if effective_bytes >= block_bytes:
        policy_state = "block"
        reason = "browser_stream_payload_blocked"
    elif effective_bytes >= warn_bytes:
        policy_state = "throttle"
        reason = "browser_stream_payload_pressure"
    if policy_state == "ok":
        return True
    if not owner:
        _log.warning(
            "webio stream dropped by payload guard webspace=%s receiver=%s bytes=%s fanout=%s effective_bytes=%s reason=%s owner=unknown",
            webspace_id,
            receiver,
            payload_bytes,
            fanout_total,
            effective_bytes,
            reason,
        )
        return False
    try:
        from adaos.services.yjs.owner_guard import admit_owner_work

        admission = admit_owner_work(
            webspace_id=webspace_id,
            owner=owner,
            root_names=["stream"],
            path=f"stream/{receiver}",
            source="router.webio_stream",
            channel="webio.stream",
            work_kind="browser_stream",
            tool=f"{owner}:stream:{receiver}",
            policy={
                "policy_state": policy_state,
                "reason": reason,
                "observed_state": "critical" if policy_state == "block" else "high",
                "payload_bytes": payload_bytes,
                "fanout_total": fanout_total,
                "effective_bytes": effective_bytes,
                "blocked_roots": ["stream"] if policy_state == "block" else [],
                "throttled_roots": ["stream"] if policy_state == "throttle" else [],
            },
        )
        if not bool(admission.get("allowed", True)):
            _log.warning(
                "webio stream denied by owner guard webspace=%s receiver=%s owner=%s bytes=%s fanout=%s effective_bytes=%s reason=%s retry_after_s=%s",
                webspace_id,
                receiver,
                admission.get("owner") or owner,
                payload_bytes,
                fanout_total,
                effective_bytes,
                admission.get("reason") or reason,
                admission.get("retry_after_s") or 0,
            )
            return False
        if bool(admission.get("throttled")):
            _log.warning(
                "webio stream allowed under pressure webspace=%s receiver=%s owner=%s bytes=%s fanout=%s effective_bytes=%s reason=%s",
                webspace_id,
                receiver,
                admission.get("owner") or owner,
                payload_bytes,
                fanout_total,
                effective_bytes,
                admission.get("reason") or reason,
            )
        return True
    except Exception:
        _log.warning(
            "webio stream dropped after guard failure webspace=%s receiver=%s owner=%s bytes=%s fanout=%s effective_bytes=%s reason=%s",
            webspace_id,
            receiver,
            owner,
            payload_bytes,
            fanout_total,
            effective_bytes,
            reason,
            exc_info=True,
        )
        return False


class RouterService:
    def __init__(self, eventbus: LocalEventBus, base_dir: Path) -> None:
        self.bus = eventbus
        self.base_dir = base_dir
        self._started = False
        self._stop_watch: Callable[[], None] | None = None
        self._rules: list[dict[str, Any]] = []
        self._subscribed = False
        self._vlog = logging.getLogger("adaos.router.voice_chat")
        self._tg_reply_via_root_http = str(os.getenv("HUB_TG_REPLY_VIA_ROOT_HTTP") or "").strip() == "1"
        self._media_route_webspaces: set[str] = set()
        self._notify_tasks: set[asyncio.Task[None]] = set()

    def _router_yjs_write_meta(self):
        return ystore_write_metadata(
            root_names=["data"],
            source="router.service",
            owner="core:router",
            channel="core.router.async",
        )

    def _pick_target_node(self, desired_io: str, this_node: str) -> str:
        node = this_node
        for r in self._rules:
            try:
                target = r.get("target") or {}
                if str(target.get("io_type") or "stdout").lower() == desired_io.lower():
                    nid = target.get("node_id")
                    if nid == "this" or not nid:
                        node = this_node
                    else:
                        node = str(nid)
                    break
            except Exception:
                continue
        return node

    def _has_rule_for(self, desired_io: str) -> bool:
        for r in self._rules:
            try:
                target = r.get("target") or {}
                if str(target.get("io_type") or "").lower() == desired_io.lower():
                    return True
            except Exception:
                continue
        return False

    def _event_targets_local_node(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        target_node_id = str(
            payload.get("target_node_id")
            or payload.get("node_id")
            or meta.get("target_node_id")
            or meta.get("node_target_id")
            or ""
        ).strip()
        if not target_node_id:
            return True
        try:
            local_node_id = str(get_ctx().config.node_id or "").strip()
        except Exception:
            local_node_id = ""
        return not local_node_id or target_node_id == local_node_id

    def _event_originates_from_remote_member(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        origin_node_id = str(meta.get("subnet_origin_node_id") or "").strip()
        if not origin_node_id:
            return False
        try:
            local_node_id = str(get_ctx().config.node_id or "").strip()
        except Exception:
            local_node_id = ""
        if local_node_id and origin_node_id == local_node_id:
            return False
        return not self._event_targets_local_node(payload)

    async def _on_event(self, ev: Event) -> None:
        try:
            task = asyncio.create_task(self._handle_notify_event(ev), name=f"router-ui-notify:{str(ev.type or 'ui.notify')}")
        except Exception:
            await self._handle_notify_event(ev)
            return
        self._notify_tasks.add(task)

        def _forget(done: asyncio.Task[None]) -> None:
            self._notify_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.getLogger("adaos.router").warning("router: ui.notify background delivery failed", exc_info=True)

        task.add_done_callback(_forget)

    async def _handle_notify_event(self, ev: Event) -> None:
        payload = ev.payload or {}
        text = (payload or {}).get("text")
        if not isinstance(text, str) or not text:
            return
        meta = payload.get("_meta") if isinstance(payload, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        is_tg = str(meta.get("io_type") or "").lower() == "telegram"
        chat_id = meta.get("chat_id") if is_tg else None
        is_tg_chat = isinstance(chat_id, str) and bool(chat_id.strip())

        # If this came from a chat platform (telegram), reply back into that chat via tg.output.*.
        # This path does not depend on route rules and is meant to be "request/response" style.
        try:
            if is_tg and is_tg_chat and not self._tg_reply_via_root_http:
                bot_id = meta.get("bot_id")
                if not isinstance(bot_id, str) or not bot_id.strip():
                    bot_id = "main-bot"
                hub_id = meta.get("hub_id")
                if not isinstance(hub_id, str) or not hub_id.strip():
                    hub_id = get_ctx().config.subnet_id
                out_payload = {
                    "target": {"bot_id": bot_id, "hub_id": hub_id, "chat_id": chat_id.strip()},
                    "messages": [{"type": "text", "text": text}],
                    "options": {"reply_to": meta.get("reply_to")} if meta.get("reply_to") else None,
                }
                self.bus.publish(
                    Event(
                        type=f"tg.output.{bot_id}.chat.{chat_id.strip()}",
                        source="router",
                        ts=time.time(),
                        payload=out_payload,
                    )
                )
        except Exception:
            pass

        # If the notification has an explicit UI route, mirror it into that route.
        # This keeps skills UI-agnostic: they can emit ui.notify and the router
        # decides how to deliver the message to chat/TTS.
        try:
            route_id = meta.get("route_id") or meta.get("route")
            if isinstance(route_id, str) and route_id.strip():
                self.bus.publish(
                    Event(
                        type="io.out.chat.append",
                        source="router",
                        ts=time.time(),
                        payload={
                            "id": "",
                            "from": "hub",
                            "text": text,
                            "ts": time.time(),
                            "_meta": {**meta, "route_id": route_id.strip()},
                        },
                    )
                )
                self.bus.publish(
                    Event(
                        type="io.out.say",
                        source="router",
                        ts=time.time(),
                        payload={
                            "id": "",
                            "text": text,
                            "ts": time.time(),
                            "lang": str(meta.get("lang") or "ru-RU"),
                            "_meta": {**meta, "route_id": route_id.strip()},
                        },
                    )
                )
            elif is_tg and is_tg_chat and self._tg_reply_via_root_http:
                # When using Root HTTP replies, ensure we still emit io.out.chat.append even if
                # the skill didn't provide route_id/route.
                self.bus.publish(
                    Event(
                        type="io.out.chat.append",
                        source="router",
                        ts=time.time(),
                        payload={
                            "id": "",
                            "from": "hub",
                            "text": text,
                            "ts": time.time(),
                            "_meta": dict(meta),
                        },
                    )
                )
        except Exception:
            pass

        conf = get_ctx().config
        this_node = conf.node_id
        if not self._rules:
            try:
                self._rules = load_rules(self.base_dir, this_node)
            except Exception:
                pass
        # Multi-target routing: attempt telegram and stdout independently if rules exist
        did_any = False

        # Telegram route (if configured in rules)
        if self._has_rule_for("telegram") and not is_tg_chat:
            target_node_tg = self._pick_target_node("telegram", this_node)
            try:
                # Resolve hub_id for target node
                if target_node_tg == this_node:
                    hub_id = conf.subnet_id
                else:
                    directory = get_directory()
                    node = directory.get_node(target_node_tg)
                    hub_id = (node or {}).get("subnet_id")
                if not hub_id:
                    raise RuntimeError("hub_id unresolved for telegram routing")
                # Root API base
                from adaos.services.agent_context import get_ctx as _get_ctx

                api_base = getattr(_get_ctx().settings, "api_base", "https://api.inimatic.com")
                url = f"{api_base.rstrip('/')}/io/tg/send"
                # Prefix message with subnet alias (or id) for clarity
                try:
                    alias = display_subnet_alias(
                        load_subnet_alias(subnet_id=conf.subnet_id) or os.getenv("DEFAULT_HUB"),
                        conf.subnet_id,
                    )
                except Exception:
                    alias = conf.subnet_id
                prefixed_text = f"[{alias}]: {text}" if alias else text
                body = {"hub_id": hub_id, "text": prefixed_text}
                try:
                    r = await asyncio.to_thread(
                        requests.post,
                        url,
                        json=body,
                        headers={"Content-Type": "application/json"},
                        timeout=3.0,
                    )
                    if not (200 <= int(r.status_code) < 300):
                        logging.getLogger("adaos.router").warning(
                            "router: telegram send failed",
                            extra={"hub_id": hub_id, "status": r.status_code, "body": (r.text or "")[:300]},
                        )
                    else:
                        logging.getLogger("adaos.router").info(
                            "router: telegram sent", extra={"hub_id": hub_id, "status": r.status_code}
                        )
                except Exception as pe:
                    logging.getLogger("adaos.router").warning("router: telegram request failed", extra={"hub_id": hub_id, "error": str(pe)})
                    raise
                did_any = True
            except Exception:
                # swallow to allow stdout route below
                try:
                    logging.getLogger("adaos.router").warning("router: telegram route failed; will continue with other routes")
                except Exception:
                    pass

        # Stdout route (if configured in rules)
        if self._has_rule_for("stdout"):
            target_node_out = self._pick_target_node("stdout", this_node)
            if target_node_out == this_node:
                print_text(text, node_id=this_node, origin={"source": ev.source})
                did_any = True
            else:
                # Cross-node delivery: resolve base_url and POST
                base_url = await asyncio.to_thread(self._resolve_node_base_url, target_node_out, conf.role, conf.hub_url)
                if not base_url and conf.role == "hub":
                    try:
                        directory = get_directory()
                        candidates = []
                        for n in directory.list_known_nodes():
                            if not n.get("online"):
                                continue
                            for io in (n.get("capacity") or {}).get("io", []):
                                if io.get("io_type") == "stdout":
                                    candidates.append((int(io.get("priority") or 50), n))
                                    break
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        for _, cand in candidates:
                            nid = cand.get("node_id")
                            if not nid:
                                continue
                            base_url = self._resolve_node_base_url(str(nid), conf.role, conf.hub_url)
                            if base_url:
                                break
                    except Exception:
                        base_url = None

                if base_url:
                    url = f"{base_url.rstrip('/')}/api/io/console/print"
                    headers = {"X-AdaOS-Token": conf.token or "dev-local-token", "Content-Type": "application/json"}
                    body = {"text": text, "origin": {"source": ev.source, "from": this_node}}
                    try:
                        await asyncio.to_thread(requests.post, url, json=body, headers=headers, timeout=2.5)
                        did_any = True
                    except Exception:
                        pass
                else:
                    try:
                        logging.getLogger("adaos.router").warning(f"router: stdout target {target_node_out} offline/unresolved; fallback to local print")
                    except Exception:
                        pass
                    print_text(text, node_id=this_node, origin={"source": ev.source})
                    did_any = True

        # If no route matched or everything failed, fallback to local stdout
        if not did_any:
            print_text(text, node_id=this_node, origin={"source": ev.source})

    def _resolve_node_base_url(self, node_id: str, role: str, hub_url: str | None) -> str | None:
        try:
            if role == "hub":
                directory = get_directory()
                if not directory.is_online(node_id):
                    return None
                return directory.get_node_base_url(node_id)
            # member: ask hub
            if not hub_url:
                return None
            url = f"{hub_url.rstrip('/')}/api/subnet/nodes/{node_id}"
            token = load_config().token or "dev-local-token"
            r = requests.get(url, headers={"X-AdaOS-Token": token}, timeout=2.5)
            if r.status_code != 200:
                return None
            data = r.json() or {}
            node = data.get("node") or {}
            return node.get("base_url")
        except Exception:
            return None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Subscribe to ui.notify on local event bus
        if not self._subscribed:
            self.bus.subscribe("ui.notify", self._on_event)

            # ui.say routing (TTS)
            def _say_via_system(text: str) -> bool:
                try:
                    if sys.platform.startswith("win"):
                        safe = text.replace("'", "''")
                        cmd = [
                            "powershell",
                            "-NoProfile",
                            "-Command",
                            "Add-Type -AssemblyName System.Speech; "
                            "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                            f"$speak.Speak('{safe}');",
                        ]
                        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
                    if sys.platform == "darwin" and shutil.which("say"):
                        subprocess.run(["say", text], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
                    if shutil.which("espeak"):
                        subprocess.run(["espeak", text], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
                except Exception:
                    return False
                return False

            def _say_sync(ev: Event, text: str, voice: Any) -> None:
                """
                Execute TTS routing in a worker thread so it never blocks the event loop.
                This is important because ui.say can be emitted early during boot and any
                blocking work (subprocess/requests/TTS engines) can stall NATS WS handshakes.
                """
                conf = load_config()
                this_node = conf.node_id
                target_node = self._pick_target_node("say", this_node)
                base_url = self._resolve_node_base_url(target_node, conf.role, conf.hub_url)
                token = conf.token or "dev-local-token"
                if base_url and target_node != this_node:
                    try:
                        requests.post(
                            f"{base_url.rstrip('/')}/api/say",
                            json={"text": text, "voice": voice},
                            headers={"X-AdaOS-Token": token, "Content-Type": "application/json"},
                            timeout=3.0,
                        )
                        return
                    except Exception:
                        pass
                # local fallback via API if self base_url known, else direct adapter
                self_url = os.environ.get("ADAOS_SELF_BASE_URL")
                if self_url:
                    try:
                        requests.post(
                            f"{self_url.rstrip('/')}/api/say",
                            json={"text": text, "voice": voice},
                            headers={"X-AdaOS-Token": token, "Content-Type": "application/json"},
                            timeout=3.0,
                        )
                        return
                    except Exception:
                        pass
                try:
                    mode = get_tts_backend()
                    adapter = NativeTTS() if mode == "native" else RhasspyTTSAdapter()
                    adapter.say(text)
                    return
                except Exception:
                    if not _say_via_system(text):
                        print_text(text, node_id=this_node, origin={"source": ev.source})

            async def _on_say(ev: Event) -> None:
                payload = ev.payload or {}
                text = (payload or {}).get("text")
                if not isinstance(text, str) or not text.strip():
                    return
                voice = (payload or {}).get("voice")
                try:
                    await asyncio.to_thread(_say_sync, ev, text.strip(), voice)
                except Exception:
                    try:
                        conf = get_ctx().config
                        print_text(text.strip(), node_id=conf.node_id, origin={"source": ev.source})
                    except Exception:
                        pass

            self.bus.subscribe("ui.say", _on_say)
            self._subscribed = True

        # ------------------------------------------------------------
        # Web voice chat routing (per-webspace)
        # ------------------------------------------------------------

        def _coerce_y(node: Any) -> Any:
            if isinstance(node, dict):
                return {str(k): _coerce_y(v) for k, v in node.items()}
            if isinstance(node, Y.YMap):
                return {str(k): _coerce_y(node.get(k)) for k in list(node.keys())}
            if isinstance(node, Y.YArray):
                return [_coerce_y(it) for it in node]
            return node

        def _coerce_webspace_id(value: Any) -> str:
            return coerce_webspace_id(value, fallback="default")

        def _resolve_webspace_ids_basic(payload: dict | None) -> list[str]:
            if not isinstance(payload, dict):
                return ["default"]

            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            raw_ids = (meta or {}).get("webspace_ids")
            if isinstance(raw_ids, list):
                out: list[str] = []
                for v in raw_ids:
                    s = _coerce_webspace_id(v)
                    if not s:
                        continue
                    if s not in out:
                        out.append(s)
                if out:
                    return out

            raw = (
                (meta or {}).get("webspace_id")
                or (meta or {}).get("workspace_id")
                or payload.get("webspace_id")
                or payload.get("workspace_id")
                or "default"
            )
            return [_coerce_webspace_id(raw)]

        _route_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

        async def _resolve_webspace_ids(payload: dict | None) -> list[str]:
            base_ids = _resolve_webspace_ids_basic(payload)
            if not isinstance(payload, dict):
                return base_ids

            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            # If explicit targets are provided, keep them authoritative.
            raw_ids = (meta or {}).get("webspace_ids")
            if isinstance(raw_ids, list) and raw_ids:
                return base_ids

            route_id = (meta or {}).get("route_id") or (meta or {}).get("route")
            if not isinstance(route_id, str) or not route_id.strip():
                return base_ids
            route_id = route_id.strip()
            src_ws = base_ids[0] if base_ids else "default"

            cached = _route_cache.get((src_ws, route_id))
            now = time.time()
            if cached and (now - cached[0]) < 1.0:
                return cached[1]

            try:
                async with async_read_ydoc(src_ws) as ydoc:
                    data = ydoc.get_map("data")
                    routing = _coerce_y(data.get("routing")) or {}
                    routes = routing.get("routes") if isinstance(routing, dict) else {}
                    if not isinstance(routes, dict):
                        routes = {}
                    entry = routes.get(route_id)
                    targets: list[str] = []
                    if isinstance(entry, list):
                        targets = [str(x).strip() for x in entry if str(x).strip()]
                    elif isinstance(entry, dict):
                        raw = entry.get("webspace_ids") or entry.get("targets")
                        if isinstance(raw, list):
                            targets = [str(x).strip() for x in raw if str(x).strip()]
                    if targets:
                        # De-dup while preserving order.
                        dedup: list[str] = []
                        for t in targets:
                            if t not in dedup:
                                dedup.append(t)
                        _route_cache[(src_ws, route_id)] = (now, dedup)
                        return dedup
            except Exception:
                pass

            _route_cache[(src_ws, route_id)] = (now, base_ids)
            return base_ids

        def _voice_chat_data_path(target_node_id: str | None) -> str:
            return node_scope_data_path("data/voice_chat", str(target_node_id or "").strip())

        def _local_node_id() -> str:
            try:
                return str(get_ctx().config.node_id or "").strip()
            except Exception:
                return ""

        def _resolve_voice_target_node_id(
            payload: Any,
            meta: dict | None = None,
            *,
            default_local: bool = False,
        ) -> str | None:
            payload_dict = payload if isinstance(payload, dict) else {}
            meta_dict = meta if isinstance(meta, dict) else {}
            token = str(
                payload_dict.get("target_node_id")
                or payload_dict.get("node_id")
                or meta_dict.get("target_node_id")
                or meta_dict.get("node_target_id")
                or ""
            ).strip()
            if not token and default_local:
                token = _local_node_id()
            return token or None

        def _read_voice_chat_state(data_map: Any, target_node_id: str | None) -> dict:
            current = data_map.to_json() if hasattr(data_map, "to_json") else {}
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except Exception:
                    current = {}
            if not isinstance(current, dict):
                return {}
            path = _voice_chat_data_path(target_node_id)
            segments = [segment for segment in path.split("/") if segment]
            cursor: Any = current
            for segment in segments[1:]:
                if not isinstance(cursor, dict):
                    return {}
                cursor = cursor.get(segment)
            return dict(cursor) if isinstance(cursor, dict) else {}

        def _write_voice_chat_state(data_map: Any, txn: Any, target_node_id: str | None, value: dict) -> None:
            path = _voice_chat_data_path(target_node_id)
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) < 2:
                return
            top_key = segments[1]
            if len(segments) == 2:
                data_map.set(txn, top_key, value)
                return
            current_top = data_map.get(top_key)
            changed, merged = _merge_nested_path(current_top, segments[2:], value)
            if changed:
                data_map.set(txn, top_key, merged)

        async def _mutate_data_map(
            webspace_id: str,
            mutator: Callable[[Any, Any], None],
            *,
            source: str = "router.service",
            channel: str = "core.router.live_room",
        ) -> None:
            def _apply(ydoc: Any, txn: Any) -> None:
                mutator(ydoc.get_map("data"), txn)

            if mutate_live_room(
                webspace_id,
                _apply,
                root_names=["data"],
                source=source,
                owner="core:router",
                channel=channel,
            ):
                return
            async with self._router_yjs_write_meta():
                async with async_get_ydoc(
                    webspace_id,
                    publish_live_room=False,
                    load_mark_roots=["data"],
                    write_source=source,
                    write_owner="core:router",
                    write_channel="core.router.async",
                ) as ydoc:
                    with ydoc.begin_transaction() as txn:
                        _apply(ydoc, txn)

        async def _ensure_voice_chat_state(webspace_id: str, target_node_id: str | None = None) -> None:
            def _mutator(data_map: Any, txn: Any) -> None:
                current = _read_voice_chat_state(data_map, target_node_id)
                if isinstance(current, dict) and isinstance(current.get("messages"), list):
                    return
                _write_voice_chat_state(
                    data_map,
                    txn,
                    target_node_id,
                    {
                        "messages": [],
                        "last_refresh_ts": time.time(),
                    },
                )

            await _mutate_data_map(webspace_id, _mutator, channel="core.router.voice_chat.live_room")

        async def _append_voice_chat_message(
            webspace_id: str,
            msg: dict,
            target_node_id: str | None = None,
        ) -> None:
            count = 0

            def _mutator(data_map: Any, txn: Any) -> None:
                nonlocal count
                current = _read_voice_chat_state(data_map, target_node_id)
                messages = []
                if isinstance(current, dict) and isinstance(current.get("messages"), list):
                    messages = list(current.get("messages") or [])
                messages.append(msg)
                if len(messages) > 60:
                    messages = messages[-60:]
                count = len(messages)
                _write_voice_chat_state(
                    data_map,
                    txn,
                    target_node_id,
                    {
                        "messages": messages,
                        "last_refresh_ts": time.time(),
                    },
                )

            await _mutate_data_map(webspace_id, _mutator, channel="core.router.voice_chat.live_room")
            try:
                self._vlog.debug(
                    "voice_chat.append webspace=%s node_id=%s count=%d last_from=%s last_text=%r",
                    webspace_id,
                    str(target_node_id or "").strip() or None,
                    count,
                    msg.get("from"),
                    msg.get("text"),
                )
            except Exception:
                pass

        def _voice_intent_demo_enabled() -> bool:
            return str(os.getenv("ADAOS_VOICE_CHAT_INTENT_DEMO") or "1").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        def _voice_intent_locale(meta: dict[str, Any]) -> str:
            for key in ("locale", "request_locale", "language", "lang"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value.strip()
                    return raw.split("-", 1)[0].split("_", 1)[0] or raw
            return "ru"

        def _format_voice_intent_demo(result: dict[str, Any]) -> str:
            accepted = bool(result.get("accepted"))
            intent = str(result.get("intent") or "not_obtained")
            via = str(result.get("via") or "neural")
            parts = [
                f"Intent detector: {intent}",
                f"via={via}",
                f"accepted={str(accepted).lower()}",
            ]
            confidence = result.get("confidence")
            if isinstance(confidence, (int, float)):
                parts.append(f"confidence={confidence:.3f}")
            slots = result.get("slots")
            if isinstance(slots, dict) and slots:
                parts.append("slots=" + json.dumps(slots, ensure_ascii=False, sort_keys=True, default=str))
            reason = result.get("reason")
            if isinstance(reason, str) and reason.strip() and not accepted:
                parts.append(f"reason={reason.strip()}")
            return " | ".join(parts)

        async def _append_voice_intent_demo(
            webspace_id: str,
            text: str,
            meta: dict[str, Any],
            target_node_id: str | None,
        ) -> None:
            if not _voice_intent_demo_enabled():
                return
            locale = _voice_intent_locale(meta)
            request_id = str(meta.get("message_id") or meta.get("id") or _make_id("intent"))
            demo_meta = {
                **meta,
                "webspace_id": webspace_id,
                "route_id": "voice_chat",
                "voice_chat_intent_demo": True,
            }
            try:
                from adaos.services.nlu import neural_service_bridge

                result = await neural_service_bridge.parse_text(
                    text,
                    webspace_id=webspace_id,
                    request_id=request_id,
                    meta=demo_meta,
                    locale=locale,
                    preferred_locales=[locale],
                    record_usage_stats=False,
                )
                msg_text = _format_voice_intent_demo(result if isinstance(result, dict) else {})
            except Exception as exc:
                msg_text = f"Intent detector unavailable: {type(exc).__name__}: {exc}"
            await _append_voice_chat_message(
                webspace_id,
                {
                    "id": _make_id("intent"),
                    "from": "hub",
                    "text": msg_text,
                    "ts": time.time(),
                },
                target_node_id,
            )

        async def _ensure_tts_state(webspace_id: str) -> None:
            def _mutator(data_map: Any, txn: Any) -> None:
                current = data_map.get("tts")
                if isinstance(current, dict) and isinstance(current.get("queue"), list):
                    return
                data_map.set(txn, "tts", {"queue": []})

            await _mutate_data_map(webspace_id, _mutator, channel="core.router.tts.live_room")

        async def _append_tts_queue_item(webspace_id: str, item: dict) -> None:
            def _mutator(data_map: Any, txn: Any) -> None:
                current = data_map.get("tts")
                queue = []
                if isinstance(current, dict) and isinstance(current.get("queue"), list):
                    queue = list(current.get("queue") or [])
                queue.append(item)
                if len(queue) > 50:
                    queue = queue[-50:]
                data_map.set(txn, "tts", {"queue": queue})

            await _mutate_data_map(webspace_id, _mutator, channel="core.router.tts.live_room")

        def _publish_webio_stream_event(
            webspace_id: str,
            receiver: str,
            payload: dict[str, Any],
            *,
            source: str,
            ts: float,
        ) -> None:
            ws = coerce_webspace_id(webspace_id, fallback="default")
            receiver_id = str(receiver or "").strip()
            if not receiver_id:
                return
            node_id = str(
                payload.get("node_id")
                or payload.get("source_node_id")
                or (
                    payload.get("_meta", {}).get("node_id")
                    if isinstance(payload.get("_meta"), dict)
                    else ""
                )
                or ""
            ).strip()
            topics = [f"webio.stream.{ws}.{receiver_id}"]
            if node_id:
                topics.append(f"webio.stream.{ws}.nodes.{node_id}.{receiver_id}")
                topics.append(f"webio.stream.nodes.{node_id}.{receiver_id}")
            for topic in topics:
                self.bus.publish(
                    Event(
                        type=topic,
                        source=source,
                        ts=ts,
                        payload=payload,
                    )
                )

        def _coerce_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return False

        def _coerce_int(value: Any) -> int:
            try:
                return int(value or 0)
            except Exception:
                return 0

        def _coerce_float(value: Any) -> float | None:
            try:
                if value is None or value == "":
                    return None
                return float(value)
            except Exception:
                return None

        async def _ensure_media_state(webspace_id: str) -> None:
            def _mutator(data_map: Any, txn: Any) -> None:
                current = _coerce_y(data_map.get("media"))
                if isinstance(current, dict) and isinstance(current.get("route"), dict):
                    return
                next_state = dict(current) if isinstance(current, dict) else {}
                next_state.setdefault("route", {})
                data_map.set(txn, "media", next_state)

            await _mutate_data_map(webspace_id, _mutator, channel="core.router.media.live_room")

        async def _set_media_route_state(webspace_id: str, route_state: dict[str, Any]) -> None:
            def _mutator(data_map: Any, txn: Any) -> None:
                current = _coerce_y(data_map.get("media"))
                next_state = dict(current) if isinstance(current, dict) else {}
                next_state["route"] = route_state
                data_map.set(txn, "media", next_state)

            await _mutate_data_map(webspace_id, _mutator, channel="core.router.media.live_room")

        async def _get_media_route_state(webspace_id: str) -> dict[str, Any] | None:
            async with async_read_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                current = _coerce_y(data_map.get("media"))
                if not isinstance(current, dict):
                    return None
                route = current.get("route")
                return dict(route) if isinstance(route, dict) else None

        def _remember_media_webspaces(webspace_ids: list[str] | None) -> None:
            for item in list(webspace_ids or []):
                token = str(item or "").strip()
                if token:
                    self._media_route_webspaces.add(token)

        def _active_browser_session_totals() -> tuple[int, int]:
            try:
                from adaos.services.yjs.gateway_ws import active_browser_session_snapshot

                snapshot = active_browser_session_snapshot()
            except Exception:
                return (0, 0)
            peers = snapshot.get("peers") if isinstance(snapshot.get("peers"), list) else []
            total = 0
            connected = 0
            for item in peers:
                if not isinstance(item, dict):
                    continue
                total += 1
                if str(item.get("connection_state") or "").strip().lower() == "connected":
                    connected += 1
            return (total, connected)

        def _route_ability_available(route_state: dict[str, Any], topology_id: str) -> bool:
            capabilities = route_state.get("capabilities") if isinstance(route_state.get("capabilities"), dict) else {}
            abilities = capabilities.get("ability") if isinstance(capabilities.get("ability"), dict) else {}
            entry = abilities.get(topology_id) if isinstance(abilities.get(topology_id), dict) else {}
            return _coerce_bool(entry.get("available"))

        def _route_target_member_id(route_state: dict[str, Any]) -> str:
            preferred_member_id = str(route_state.get("preferred_member_id") or "").strip()
            if preferred_member_id:
                return preferred_member_id
            producer_target = route_state.get("producer_target") if isinstance(route_state.get("producer_target"), dict) else {}
            return str(producer_target.get("member_id") or "").strip()

        def _route_signature(route_state: dict[str, Any] | None) -> tuple[str, str, str, str, str]:
            state = route_state if isinstance(route_state, dict) else {}
            producer_target = state.get("producer_target") if isinstance(state.get("producer_target"), dict) else {}
            return (
                str(state.get("active_route") or "").strip(),
                str(state.get("delivery_topology") or "").strip(),
                _route_target_member_id(state),
                str(producer_target.get("kind") or "").strip(),
                str(producer_target.get("webspace_id") or "").strip(),
            )

        def _build_media_route_attempt(
            previous_route_state: dict[str, Any] | None,
            normalized_route_state: dict[str, Any],
            *,
            cause: str,
            ts: float,
            observed_failure: str | None = None,
        ) -> dict[str, Any]:
            previous = previous_route_state if isinstance(previous_route_state, dict) else {}
            previous_attempt = _coerce_y(previous.get("attempt"))
            previous_attempt = dict(previous_attempt) if isinstance(previous_attempt, dict) else {}
            previous_signature = _route_signature(previous)
            next_signature = _route_signature(normalized_route_state)
            has_previous_selection = any(previous_signature)
            route_changed = next_signature != previous_signature
            sequence = _coerce_int(previous_attempt.get("sequence"))
            if sequence <= 0:
                sequence = 1
            elif route_changed and has_previous_selection:
                sequence += 1
            switch_total = _coerce_int(previous_attempt.get("switch_total"))
            if route_changed and has_previous_selection:
                switch_total += 1
            selected_at = _coerce_float(previous_attempt.get("selected_at"))
            if selected_at is None or (route_changed and has_previous_selection):
                selected_at = ts
            last_switch_at = _coerce_float(previous_attempt.get("last_switch_at"))
            if route_changed and has_previous_selection:
                last_switch_at = ts
            previous_route = str(previous.get("active_route") or "").strip()
            previous_delivery_topology = str(previous.get("delivery_topology") or "").strip()
            previous_member_id = _route_target_member_id(previous)
            producer_target = (
                normalized_route_state.get("producer_target")
                if isinstance(normalized_route_state.get("producer_target"), dict)
                else {}
            )
            current_failure = str(observed_failure or "").strip() or None
            if current_failure is None:
                current_failure = str(previous_attempt.get("observed_failure") or "").strip() or None

            attempt = {
                "sequence": sequence,
                "state": "selected" if str(normalized_route_state.get("active_route") or "").strip() else "unavailable",
                "active_route": normalized_route_state.get("active_route"),
                "delivery_topology": normalized_route_state.get("delivery_topology"),
                "preferred_route": normalized_route_state.get("preferred_route"),
                "preferred_member_id": normalized_route_state.get("preferred_member_id"),
                "producer_target": dict(producer_target) if producer_target else None,
                "selection_reason": normalized_route_state.get("selection_reason"),
                "degradation_reason": normalized_route_state.get("degradation_reason"),
                "refresh_cause": cause,
                "observed_failure": current_failure,
                "switch_total": switch_total,
                "selected_at": selected_at,
                "last_switch_at": last_switch_at,
            }
            if route_changed and has_previous_selection:
                if previous_route:
                    attempt["previous_route"] = previous_route
                if previous_delivery_topology:
                    attempt["previous_delivery_topology"] = previous_delivery_topology
                if previous_member_id:
                    attempt["previous_member_id"] = previous_member_id
            else:
                prior_route = str(previous_attempt.get("previous_route") or "").strip()
                prior_topology = str(previous_attempt.get("previous_delivery_topology") or "").strip()
                prior_member = str(previous_attempt.get("previous_member_id") or "").strip()
                if prior_route:
                    attempt["previous_route"] = prior_route
                if prior_topology:
                    attempt["previous_delivery_topology"] = prior_topology
                if prior_member:
                    attempt["previous_member_id"] = prior_member
            return attempt

        def _refresh_media_route_payload(route_state: dict[str, Any], *, cause: str, observed_failure: str | None = None) -> dict[str, Any]:
            member_browser = (
                route_state.get("member_browser_direct")
                if isinstance(route_state.get("member_browser_direct"), dict)
                else {}
            )
            browser_session_total, connected_browser_session_total = _active_browser_session_totals()
            payload: dict[str, Any] = {
                "need": str(route_state.get("route_intent") or "scenario_response_media"),
                "producer_preference": str(route_state.get("producer_preference") or ""),
                "direct_local_ready": _route_ability_available(route_state, "local_http"),
                "root_routed_ready": _route_ability_available(route_state, "root_media_relay"),
                "hub_webrtc_ready": _route_ability_available(route_state, "hub_webrtc_loopback"),
                "browser_session_total": browser_session_total,
                "connected_browser_session_total": connected_browser_session_total,
                "refresh_cause": cause,
            }
            if member_browser:
                payload["member_browser_direct"] = {}
                if "admitted" in member_browser:
                    payload["member_browser_direct"]["admitted"] = _coerce_bool(member_browser.get("admitted"))
            monitoring = route_state.get("monitoring") if isinstance(route_state.get("monitoring"), dict) else {}
            existing_failure = str(monitoring.get("observed_failure") or "").strip()
            if observed_failure:
                payload["observed_failure"] = observed_failure
            elif existing_failure:
                payload["observed_failure"] = existing_failure
            return payload

        async def _refresh_media_route_for_webspace(
            webspace_id: str,
            *,
            cause: str,
            observed_failure: str | None = None,
        ) -> bool:
            route_state = await _get_media_route_state(webspace_id)
            if not isinstance(route_state, dict):
                return False
            if str(route_state.get("route_administrator") or "router").strip().lower() not in {"", "router"}:
                return False
            payload = _refresh_media_route_payload(
                route_state,
                cause=cause,
                observed_failure=observed_failure,
            )
            payload["ts"] = time.time()
            next_route_state = _resolve_media_route_state(
                payload,
                webspace_id=webspace_id,
                previous_route_state=route_state,
            )
            if not isinstance(next_route_state, dict):
                return False
            monitoring = next_route_state.get("monitoring") if isinstance(next_route_state.get("monitoring"), dict) else {}
            if monitoring:
                monitoring = dict(monitoring)
                monitoring["refresh_cause"] = cause
                next_route_state["monitoring"] = monitoring
            await _set_media_route_state(webspace_id, next_route_state)
            return True

        async def _refresh_media_routes(
            *,
            webspace_ids: list[str] | None = None,
            cause: str,
            observed_failure: str | None = None,
        ) -> None:
            targets = [
                str(item or "").strip()
                for item in list(webspace_ids or self._media_route_webspaces)
                if str(item or "").strip()
            ]
            if not targets:
                return
            _remember_media_webspaces(targets)
            for ws in targets:
                try:
                    await _refresh_media_route_for_webspace(
                        ws,
                        cause=cause,
                        observed_failure=observed_failure,
                    )
                except Exception:
                    continue

        def _resolve_media_route_state(
            payload: dict[str, Any],
            *,
            webspace_id: str,
            previous_route_state: dict[str, Any] | None = None,
        ) -> dict[str, Any] | None:
            raw_route = payload.get("route")
            if not isinstance(raw_route, dict) and isinstance(payload.get("route_intent"), dict):
                raw_route = payload.get("route_intent")

            route_state = _coerce_y(raw_route) if isinstance(raw_route, dict) else None
            member_browser = payload.get("member_browser_direct")
            member_browser = member_browser if isinstance(member_browser, dict) else {}
            current_browser_session_total, current_connected_browser_session_total = _active_browser_session_totals()
            route_producer_target = (
                route_state.get("producer_target")
                if isinstance(route_state, dict) and isinstance(route_state.get("producer_target"), dict)
                else {}
            )
            preferred_member_id = str(payload.get("preferred_member_id") or "").strip()
            if not preferred_member_id and isinstance(route_state, dict):
                preferred_member_id = str(route_state.get("preferred_member_id") or "").strip()
            if not preferred_member_id:
                preferred_member_id = str(route_producer_target.get("member_id") or "").strip()
            raw_candidate_members = (
                member_browser.get("candidate_members")
                if isinstance(member_browser.get("candidate_members"), list)
                else payload.get("candidate_member_ids")
            )
            candidate_member_ids = (
                [
                    str(item or "").strip()
                    for item in raw_candidate_members
                    if str(item or "").strip()
                ]
                if isinstance(raw_candidate_members, list)
                else []
            )
            admitted_member_browser = (
                _coerce_bool(member_browser.get("admitted"))
                if member_browser and "admitted" in member_browser
                else _coerce_bool(payload.get("member_browser_direct_admitted"))
            )
            auto_member_browser: dict[str, Any] = {}
            if not preferred_member_id or not candidate_member_ids:
                try:
                    from adaos.services.media_capability import member_browser_direct_foundation

                    auto_member_browser = member_browser_direct_foundation(
                        browser_session_total=(
                            _coerce_int(member_browser.get("browser_session_total"))
                            if member_browser and "browser_session_total" in member_browser
                            else (
                                _coerce_int(payload.get("browser_session_total"))
                                if "browser_session_total" in payload
                                else current_browser_session_total
                            )
                        ),
                        connected_browser_session_total=(
                            _coerce_int(member_browser.get("connected_browser_session_total"))
                            if member_browser and "connected_browser_session_total" in member_browser
                            else (
                                _coerce_int(payload.get("connected_browser_session_total"))
                                if "connected_browser_session_total" in payload
                                else current_connected_browser_session_total
                            )
                        ),
                        admitted=admitted_member_browser,
                    )
                except Exception:
                    auto_member_browser = {}
            if not preferred_member_id:
                preferred_member_id = str(auto_member_browser.get("preferred_member_id") or "").strip()
            if not candidate_member_ids:
                candidate_member_ids = [
                    str(item or "").strip()
                    for item in list(auto_member_browser.get("candidate_members") or [])
                    if str(item or "").strip()
                ]

            if route_state is None:
                route_state = resolve_media_route_intent(
                    need=str(payload.get("need") or payload.get("route_intent") or "scenario_response_media"),
                    target_webspace_id=webspace_id,
                    producer_preference=str(payload.get("producer_preference") or ""),
                    preferred_member_id=preferred_member_id or None,
                    candidate_member_ids=candidate_member_ids,
                    direct_local_ready=_coerce_bool(payload.get("direct_local_ready")),
                    root_routed_ready=_coerce_bool(payload.get("root_routed_ready")),
                    hub_webrtc_ready=_coerce_bool(payload.get("hub_webrtc_ready")),
                    member_browser_direct_possible=(
                        _coerce_bool(member_browser.get("possible"))
                        if member_browser and "possible" in member_browser
                        else (
                            _coerce_bool(payload.get("member_browser_direct_possible"))
                            if "member_browser_direct_possible" in payload
                            else _coerce_bool(auto_member_browser.get("possible"))
                        )
                    ),
                    member_browser_direct_admitted=(
                        _coerce_bool(member_browser.get("admitted"))
                        if member_browser and "admitted" in member_browser
                        else (
                            _coerce_bool(payload.get("member_browser_direct_admitted"))
                            if "member_browser_direct_admitted" in payload
                            else _coerce_bool(auto_member_browser.get("admitted"))
                        )
                    ),
                    member_browser_direct_reason=(
                        str(member_browser.get("reason") or "").strip()
                        or str(payload.get("member_browser_direct_reason") or "").strip()
                        or str(auto_member_browser.get("reason") or "").strip()
                        or None
                    ),
                    candidate_member_total=(
                        _coerce_int(member_browser.get("candidate_member_total"))
                        if member_browser and "candidate_member_total" in member_browser
                        else (
                            _coerce_int(payload.get("candidate_member_total"))
                            if "candidate_member_total" in payload
                            else _coerce_int(auto_member_browser.get("candidate_member_total"))
                        )
                    ),
                    browser_session_total=(
                        _coerce_int(member_browser.get("browser_session_total"))
                        if member_browser and "browser_session_total" in member_browser
                        else (
                            _coerce_int(payload.get("browser_session_total"))
                            if "browser_session_total" in payload
                            else _coerce_int(auto_member_browser.get("browser_session_total"))
                        )
                    ),
                    observed_failure=str(payload.get("observed_failure") or "").strip() or None,
                )

            if not isinstance(route_state, dict):
                return None

            monitoring = _coerce_y(route_state.get("monitoring"))
            monitoring = dict(monitoring) if isinstance(monitoring, dict) else {}
            observed_failure = str(payload.get("observed_failure") or "").strip()
            if observed_failure and not monitoring.get("observed_failure"):
                monitoring["observed_failure"] = observed_failure

            normalized = dict(route_state)
            normalized_member_browser = _coerce_y(normalized.get("member_browser_direct"))
            normalized_member_browser = dict(normalized_member_browser) if isinstance(normalized_member_browser, dict) else {}
            if preferred_member_id and not normalized.get("preferred_member_id"):
                normalized["preferred_member_id"] = preferred_member_id
            if candidate_member_ids and not isinstance(normalized_member_browser.get("candidate_members"), list):
                normalized_member_browser["candidate_members"] = list(candidate_member_ids)
            if preferred_member_id and not normalized_member_browser.get("preferred_member_id"):
                normalized_member_browser["preferred_member_id"] = preferred_member_id
            if candidate_member_ids and not normalized_member_browser.get("candidate_member_total"):
                normalized_member_browser["candidate_member_total"] = len(candidate_member_ids)
            if normalized_member_browser:
                normalized["member_browser_direct"] = normalized_member_browser
            refresh_cause = str(payload.get("refresh_cause") or "io.out.media.route").strip() or "io.out.media.route"
            updated_at = float(payload.get("ts") or time.time())
            effective_observed_failure = str(monitoring.get("observed_failure") or "").strip() or None
            attempt = _build_media_route_attempt(
                previous_route_state,
                normalized,
                cause=refresh_cause,
                ts=updated_at,
                observed_failure=effective_observed_failure,
            )
            normalized["attempt"] = attempt
            normalized["target_webspace_id"] = webspace_id
            normalized["route_administrator"] = "router"
            normalized["updated_at"] = updated_at
            monitoring["refresh_cause"] = refresh_cause
            monitoring["attempt_sequence"] = attempt.get("sequence")
            monitoring["switch_total"] = attempt.get("switch_total")
            monitoring["last_switch_at"] = attempt.get("last_switch_at")
            if monitoring:
                normalized["monitoring"] = monitoring
            return normalized

        def _now_ms() -> int:
            return int(time.time() * 1000)

        def _make_id(prefix: str) -> str:
            return f"{prefix}.{_now_ms()}"

        async def _on_voice_open(ev: Event) -> None:
            payload = ev.payload or {}
            if self._event_originates_from_remote_member(payload):
                return
            if not self._event_targets_local_node(payload):
                return
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            target_node_id = _resolve_voice_target_node_id(payload, meta)
            for ws in await _resolve_webspace_ids(payload):
                await _ensure_voice_chat_state(ws, target_node_id)
                await _ensure_tts_state(ws)

        async def _on_io_out_chat_append(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            if self._event_originates_from_remote_member(payload):
                try:
                    self._vlog.debug(
                        "skip mirrored remote member chat append origin=%s target=%s",
                        meta.get("subnet_origin_node_id"),
                        meta.get("target_node_id") or payload.get("target_node_id"),
                    )
                except Exception:
                    pass
                return
            if isinstance(meta, dict) and meta.get("skip_voice_chat") is True:
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return

            # Optional request/response Telegram delivery via Root HTTP (/io/tg/send).
            #
            # Disabled by default because `tg.output.*` is already bridged to Root via NATS (see bootstrap),
            # and enabling both produces duplicate Telegram messages.
            try:
                if self._tg_reply_via_root_http and str((meta or {}).get("io_type") or "").lower() == "telegram":
                    chat_id = (meta or {}).get("chat_id")
                    if isinstance(chat_id, str) and chat_id.strip():
                        bot_id = (meta or {}).get("bot_id")
                        if not isinstance(bot_id, str) or not bot_id.strip():
                            bot_id = "main-bot"
                        hub_id = (meta or {}).get("hub_id")
                        if not isinstance(hub_id, str) or not hub_id.strip():
                            hub_id = get_ctx().config.subnet_id
                        ctx = get_ctx()
                        api_base = getattr(ctx.settings, "api_base", "https://api.inimatic.com")
                        url = f"{api_base.rstrip('/')}/io/tg/send"
                        body = {"hub_id": hub_id, "bot_id": bot_id, "chat_id": chat_id.strip(), "text": text.strip()}
                        if (meta or {}).get("reply_to"):
                            body["reply_to"] = (meta or {}).get("reply_to")
                        try:
                            r = await asyncio.to_thread(
                                requests.post,
                                url,
                                json=body,
                                headers={"Content-Type": "application/json"},
                                timeout=3.0,
                            )
                            if not (200 <= int(r.status_code) < 300):
                                logging.getLogger("adaos.router").warning(
                                    "router: telegram send failed (chat reply)",
                                    extra={
                                        "hub_id": hub_id,
                                        "chat_id": chat_id.strip(),
                                        "status": r.status_code,
                                        "body": (r.text or "")[:300],
                                    },
                                )
                            else:
                                logging.getLogger("adaos.router").info(
                                    "router: telegram sent (chat reply)",
                                    extra={"hub_id": hub_id, "chat_id": chat_id.strip(), "status": r.status_code},
                                )
                        except Exception as pe:
                            logging.getLogger("adaos.router").warning(
                                "router: telegram request failed (chat reply)",
                                extra={"hub_id": hub_id, "chat_id": chat_id.strip(), "error": str(pe)},
                            )
                        return
            except Exception:
                pass

            msg = {
                "id": str(payload.get("id") or _make_id("m")),
                "from": str(payload.get("from") or "hub"),
                "text": text.strip(),
                "ts": float(payload.get("ts") or time.time()),
            }
            targets = await _resolve_webspace_ids(payload)
            route_id = str(meta.get("route_id") or meta.get("route") or "").strip()
            target_node_id = _resolve_voice_target_node_id(
                payload,
                meta,
                default_local=route_id == "voice_chat",
            )
            try:
                self._vlog.debug(
                    "io.out.chat.append received text=%r from=%s targets=%s node_id=%s",
                    msg["text"],
                    msg["from"],
                    targets,
                    target_node_id,
                )
            except Exception:
                pass
            for ws in targets:
                await _ensure_voice_chat_state(ws, target_node_id)
                await _append_voice_chat_message(ws, msg, target_node_id)

        async def _on_io_out_say(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            if self._event_originates_from_remote_member(payload):
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            item = {
                "id": str(payload.get("id") or _make_id("t")),
                "text": text.strip(),
                "ts": float(payload.get("ts") or time.time()),
            }
            if isinstance(payload.get("lang"), str) and payload.get("lang").strip():
                item["lang"] = payload.get("lang").strip()
            if isinstance(payload.get("voice"), str) and payload.get("voice").strip():
                item["voice"] = payload.get("voice").strip()
            if isinstance(payload.get("rate"), (int, float)):
                item["rate"] = float(payload.get("rate"))
            for ws in await _resolve_webspace_ids(payload):
                await _ensure_tts_state(ws)
                await _append_tts_queue_item(ws, item)

        async def _on_io_out_media_route(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            route_payload = dict(payload)
            route_payload["ts"] = float(route_payload.get("ts") or ev.ts or time.time())
            targets = await _resolve_webspace_ids(route_payload)
            _remember_media_webspaces(targets)
            for ws in targets:
                previous_route_state = await _get_media_route_state(ws)
                route_state = _resolve_media_route_state(
                    route_payload,
                    webspace_id=ws,
                    previous_route_state=previous_route_state,
                )
                if not isinstance(route_state, dict):
                    continue
                await _ensure_media_state(ws)
                await _set_media_route_state(ws, route_state)

        async def _on_io_out_stream_publish(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            receiver = str(payload.get("receiver") or "").strip()
            if not receiver:
                return
            event_ts = float(payload.get("ts") or ev.ts or time.time())
            data = payload.get("data")
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            owner = _webio_stream_owner(payload, meta)
            payload_bytes = _webio_stream_payload_bytes(
                {
                    "receiver": receiver,
                    "data": data,
                    "_meta": meta,
                }
            )
            node_id = str(
                payload.get("node_id")
                or payload.get("source_node_id")
                or meta.get("node_id")
                or meta.get("source_node_id")
                or ""
            ).strip()
            targets = await _resolve_webspace_ids(payload)
            for ws in targets:
                fanout_total = 1 + (2 if node_id else 0)
                if not _webio_stream_admit(
                    webspace_id=ws,
                    receiver=receiver,
                    owner=owner,
                    payload_bytes=payload_bytes,
                    fanout_total=fanout_total,
                ):
                    continue
                event_payload = {
                    "receiver": receiver,
                    "webspace_id": ws,
                    "data": data,
                    "ts": event_ts,
                }
                if node_id:
                    event_payload["node_id"] = node_id
                    event_payload["source_node_id"] = node_id
                if meta:
                    event_payload["_meta"] = {
                        **meta,
                        "webspace_id": ws,
                        **({"node_id": node_id, "source_node_id": node_id} if node_id else {}),
                    }
                _publish_webio_stream_event(
                    ws,
                    receiver,
                    event_payload,
                    source=str(ev.source or "router"),
                    ts=event_ts,
                )

        async def _on_browser_session_changed(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            targets = _resolve_webspace_ids_basic(payload)
            tracked_targets = [ws for ws in targets if ws in self._media_route_webspaces]
            if not tracked_targets:
                return
            observed_failure = None
            if str(payload.get("connection_state") or "").strip().lower() in {"failed", "closed", "disconnected"}:
                observed_failure = f"browser_session_{str(payload.get('connection_state') or '').strip().lower()}"
            await _refresh_media_routes(
                webspace_ids=tracked_targets,
                cause="browser.session.changed",
                observed_failure=observed_failure,
            )

        async def _on_member_media_inventory_changed(ev: Event) -> None:
            if not self._media_route_webspaces:
                return
            payload = ev.payload or {}
            observed_failure = None
            if isinstance(payload, dict) and ev.type == "subnet.member.link.down":
                node_id = str(payload.get("node_id") or "").strip()
                observed_failure = f"member_link_down:{node_id}" if node_id else "member_link_down"
            await _refresh_media_routes(
                cause=ev.type,
                observed_failure=observed_failure,
            )

        def _call_voice_chat_tool(text: str, meta: dict) -> Any:
            ctx = get_ctx()
            skills_root = ctx.paths.skills_workspace_dir()
            skills_root = skills_root() if callable(skills_root) else skills_root
            skill_dir = Path(skills_root) / "voice_chat_skill"
            prev = ctx.skill_ctx.get()
            try:
                ctx.skill_ctx.set("voice_chat_skill", skill_dir)
                # Ensure SDK io.out helpers (chat_append/say) include routing meta.
                with io_meta(meta):
                    return execute_tool(
                        skill_dir,
                        module="handlers.main",
                        attr="handle_text",
                        payload={"text": text, "_meta": meta},
                    )
            finally:
                if prev is None:
                    try:
                        ctx.skill_ctx.clear()
                    except Exception:
                        pass
                else:
                    try:
                        ctx.skill_ctx.set(prev.name, prev.path)
                    except Exception:
                        pass

        async def _on_voice_user(ev: Event) -> None:
            payload = ev.payload or {}
            if self._event_originates_from_remote_member(payload):
                return
            if not self._event_targets_local_node(payload):
                return
            try:
                target_webspaces = await _resolve_webspace_ids(payload)
            except Exception:
                target_webspaces = []
            ws = target_webspaces[0] if target_webspaces else "default"
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            text = text.strip()

            try:
                self._vlog.debug("voice.chat.user received webspace=%s text=%r", ws, text)
            except Exception:
                pass
            try:
                logging.getLogger("adaos.router.voice_chat").debug("voice.chat.user -> append+nlp webspace=%s", ws)
            except Exception:
                pass

            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            target_node_id = _resolve_voice_target_node_id(payload, meta, default_local=True)
            try:
                await _ensure_voice_chat_state(ws, target_node_id)
            except Exception:
                try:
                    logging.getLogger("adaos.router").warning("voice.chat.user: failed to ensure voice_chat state", exc_info=True)
                except Exception:
                    pass
                return

            meta = {**meta, "webspace_id": ws}
            if len(target_webspaces) > 1:
                meta["webspace_ids"] = list(target_webspaces)
            if target_node_id:
                meta.setdefault("target_node_id", target_node_id)
            # Ensure voice chat history is updated even if io.out.chat.append routing breaks.
            msg = {
                "id": _make_id("m"),
                "from": "user",
                "text": text,
                "ts": time.time(),
            }
            try:
                await _append_voice_chat_message(ws, msg, target_node_id)
            except Exception:
                pass
            try:
                self.bus.publish(
                    Event(
                        type="io.out.chat.append",
                        source="router",
                        ts=time.time(),
                        payload={
                            "id": msg["id"],
                            "from": msg["from"],
                            "text": msg["text"],
                            "ts": msg["ts"],
                            "_meta": {**meta, "route_id": "voice_chat", "skip_voice_chat": True},
                          },
                      )
                  )
            except Exception:
                pass
            # Fire-and-forget NLU detection so that text commands can be
            # mapped to scenario/skill actions via an external interpreter.
            try:
                self.bus.publish(
                    Event(
                        type="nlp.intent.detect.request",
                        source="router.voice",
                        ts=time.time(),
                        payload={
                            "text": text,
                            "webspace_id": ws,
                            "request_id": meta.get("message_id") or meta.get("id") or _make_id("nlu"),
                            "_meta": {**meta, "route_id": "voice_chat"},
                        },
                    )
                )
            except Exception:
                pass
            try:
                await _append_voice_intent_demo(ws, text, meta, target_node_id)
            except Exception:
                logging.getLogger("adaos.router.voice_chat").warning(
                    "voice.chat intent demo failed",
                    exc_info=True,
                )
            try:
                await _ensure_tts_state(ws)
            except Exception:
                pass
            # NLU pipeline + dispatcher + skills are responsible for producing
            # responses via io.out.chat.append / io.out.say.

        async def _on_nlp_intent_not_obtained(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            if self._event_originates_from_remote_member(payload):
                return
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            route_id = meta.get("route_id") or meta.get("route")
            if not isinstance(route_id, str) or not route_id.strip():
                return
            try:
                allow_teacher = bool(getattr(getattr(get_ctx().config, "root_settings", None), "llm", None).allow_nlu_teacher)  # type: ignore[attr-defined]
            except Exception:
                allow_teacher = True
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                text = ""
            if route_id.strip() == "voice_chat" and text:
                if _voice_intent_demo_enabled():
                    return
                try:
                    result = await asyncio.to_thread(_call_voice_chat_tool, text, meta)
                except Exception:
                    logging.getLogger("adaos.router.voice_chat").warning(
                        "voice.chat fallback tool failed",
                        exc_info=True,
                    )
                else:
                    if isinstance(result, dict) and bool(result.get("ok")):
                        return
            reason = payload.get("reason")
            msg_text = "Я пока не понял запрос."
            if isinstance(reason, str) and reason:
                msg_text = f"{msg_text} ({reason})"
            if text:
                msg_text = f"{msg_text} Вы сказали: «{text}»."
            if allow_teacher:
                msg_text = f"{msg_text} Я записал запрос для обучения. Открой «NLU Teacher» в Apps, чтобы посмотреть детали."
            try:
                self.bus.publish(
                    Event(
                        type="io.out.chat.append",
                        source="router.nlu",
                        ts=time.time(),
                        payload={
                            "id": "",
                            "from": "hub",
                            "text": msg_text,
                            "ts": time.time(),
                            "_meta": {**meta, "route_id": route_id.strip()},
                        },
                    )
                )
            except Exception:
                pass

        async def _on_nlp_teacher_candidate_proposed(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            route_id = meta.get("route_id") or meta.get("route")
            if not isinstance(route_id, str) or not route_id.strip():
                return

            cand = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
            req_text = cand.get("text") if isinstance(cand.get("text"), str) else ""
            kind = cand.get("kind") if isinstance(cand.get("kind"), str) else "skill"
            cdef = cand.get("candidate") if isinstance(cand.get("candidate"), dict) else {}
            name = cdef.get("name") if isinstance(cdef.get("name"), str) else ""
            desc = cdef.get("description") if isinstance(cdef.get("description"), str) else ""

            if kind == "regex_rule":
                label_kind = "правило regex"
            else:
                label_kind = "навык" if kind == "skill" else "сценарий"
            msg = "Я подготовил предложение для обучения NLU."
            if req_text:
                msg = f"Вы просили: «{req_text}».\n\nЯ подумал и добавил в план разработки кандидат: {label_kind}."
            if name:
                msg += f"\nНазвание: {name}"
            if desc:
                msg += f"\nОписание: {desc}"
            msg += "\n\nОткрой «NLU Teacher» (Apps) — там лог запроса/ответа и список кандидатов."

            try:
                self.bus.publish(
                    Event(
                        type="io.out.chat.append",
                        source="router.nlu",
                        ts=time.time(),
                        payload={
                            "id": "",
                            "from": "hub",
                            "text": msg,
                            "ts": time.time(),
                            "_meta": {**meta, "route_id": route_id.strip()},
                        },
                    )
                )
            except Exception:
                pass


        self.bus.subscribe("voice.chat.open", _on_voice_open)
        self.bus.subscribe("voice.chat.user", _on_voice_user)
        self.bus.subscribe("io.out.chat.append", _on_io_out_chat_append)
        self.bus.subscribe("io.out.say", _on_io_out_say)
        self.bus.subscribe("io.out.media.route", _on_io_out_media_route)
        self.bus.subscribe("io.out.stream.publish", _on_io_out_stream_publish)
        self.bus.subscribe("browser.session.changed", _on_browser_session_changed)
        self.bus.subscribe("subnet.member.snapshot.changed", _on_member_media_inventory_changed)
        self.bus.subscribe("subnet.member.link.up", _on_member_media_inventory_changed)
        self.bus.subscribe("subnet.member.link.down", _on_member_media_inventory_changed)
        self.bus.subscribe("capacity.changed", _on_member_media_inventory_changed)
        self.bus.subscribe("nlp.intent.not_obtained", _on_nlp_intent_not_obtained)
        self.bus.subscribe("nlp.teacher.candidate.proposed", _on_nlp_teacher_candidate_proposed)

        # Watch rules file
        def _reload(rules: list[dict]):
            self._rules = rules or []

        # Preload rules and start watcher
        try:
            node_id = get_ctx().config.node_id
        except Exception:
            # fallback: do not crash router if config is not ready yet
            node_id = ""
        self._rules = load_rules(self.base_dir, node_id)
        self._stop_watch = watch_rules(self.base_dir, node_id, _reload)

    async def stop(self) -> None:
        if self._stop_watch:
            try:
                self._stop_watch()
            except Exception:
                pass
            self._stop_watch = None
        if self._notify_tasks:
            try:
                timeout_s = max(0.0, float(os.getenv("ADAOS_ROUTER_NOTIFY_DRAIN_TIMEOUT_S") or "1.0"))
            except Exception:
                timeout_s = 1.0
            pending = list(self._notify_tasks)
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s)
            except asyncio.TimeoutError:
                for task in pending:
                    if not task.done():
                        task.cancel()
            except Exception:
                pass
            self._notify_tasks.clear()
        self._media_route_webspaces.clear()
        self._started = False
