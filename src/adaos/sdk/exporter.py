from __future__ import annotations
import inspect, json, sys, subprocess, datetime as dt
import os, importlib
from typing import Any, get_origin, get_args

from .decorators import tools_registry, tools_meta, event_payloads, emits_map

BASIC = {int: "int", float: "float", str: "str", bool: "bool", dict: "object", list: "array", type(None): "null"}
DEFAULT_EXPORT_MODULES = [
    "adaos.sdk.skills",
    "adaos.sdk.bus",
    "adaos.sdk.scenarios",
    # TODO: "adaos.sdk.llm.skill_api", "adaos.sdk.validation", ...
]


def _preload_export_modules():
    mods = os.getenv("ADAOS_SDK_EXPORT_MODULES")
    modules = (mods.split(",") if mods else []) or DEFAULT_EXPORT_MODULES
    for m in modules:
        try:
            importlib.import_module(m.strip())
        except Exception:
            # тихо пропускаем, чтобы экспорт не падал
            pass


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _type_name(tp: Any) -> str:
    if tp in BASIC:
        return BASIC[tp]
    origin = get_origin(tp)
    if origin is None:
        return getattr(tp, "__name__", str(tp))
    args = get_args(tp)
    if origin in (list, tuple):
        inner = _type_name(args[0]) if args else "any"
        return f"array[{inner}]"
    if origin is dict:
        k = _type_name(args[0]) if args else "any"
        v = _type_name(args[1]) if len(args) > 1 else "any"
        return f"object[{k}->{v}]"
    # Union/Optional
    if "Union" in str(origin):
        return " | ".join(_type_name(a) for a in args)
    return str(origin)


def _sig_human(fn) -> str:
    sig = inspect.signature(fn)
    parts = []
    for n, p in sig.parameters.items():
        ann = _type_name(p.annotation) if p.annotation is not inspect._empty else "any"
        if p.default is inspect._empty:
            parts.append(f"{n}:{ann}")
        else:
            try:
                dv = json.dumps(p.default)
            except Exception:
                dv = repr(p.default)
            parts.append(f"{n}:{ann}={dv}")
    ret = _type_name(sig.return_annotation) if sig.return_annotation is not inspect._empty else "any"
    return f"({','.join(parts)})->{ret}"


def _doc_summary(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0][:200]


def _model_schema(tp: Any) -> dict | None:
    # pydantic v1/v2 и упрощённый TypedDict
    try:
        import pydantic as p

        if hasattr(tp, "model_json_schema"):
            sch = tp.model_json_schema()
            return {"type": "object", "properties": {k: {"type": sch["properties"][k].get("type", "any")} for k in sch.get("properties", {})}, "required": sch.get("required", [])}
        if isinstance(tp, type) and issubclass(tp, p.BaseModel):
            sch = tp.schema()
            return {"type": "object", "properties": {k: {"type": sch["properties"][k].get("type", "any")} for k in sch.get("properties", {})}, "required": sch.get("required", [])}
    except Exception:
        pass
    if getattr(tp, "__annotations__", None) and "TypedDict" in str(tp):
        props = {k: {"type": _type_name(v)} for k, v in tp.__annotations__.items()}
        return {"type": "object", "properties": props, "required": list(tp.__annotations__.keys())}
    return None


def export(level: str = "std") -> dict:
    """level: mini | std | rich"""
    _preload_export_modules()
    meta = {"generated_at": _now_iso(), "git_sha": _git_sha(), "py": f"{sys.version_info.major}.{sys.version_info.minor}"}
    tools = []
    for mod, mapping in tools_registry.items():
        for public, fn in mapping.items():
            qn = f"{fn.__module__}.{fn.__name__}"
            tm = tools_meta.get(qn, {})
            item = {
                "kind": "tool",
                "name": public,
                "module": fn.__module__,
                "qualname": qn,
                "summary": tm.get("summary") or _doc_summary(fn.__doc__),
                "signature": _sig_human(fn),
                "meta": {
                    "stability": tm.get("stability", "experimental"),
                    "idempotent": tm.get("idempotent"),
                    "side_effects": tm.get("side_effects"),
                    "since": tm.get("since"),
                    "version": tm.get("version"),
                },
                "examples": tm.get("examples", []),
            }
            if tm.get("input_schema"):
                item["input_schema"] = tm["input_schema"]
            if tm.get("output_schema"):
                item["output_schema"] = tm["output_schema"]
            if level in ("std", "rich"):
                sig = inspect.signature(fn)
                args = []
                for n, p in sig.parameters.items():
                    ann = p.annotation
                    a = {"name": n, "type": _type_name(ann)}
                    if p.default is not inspect._empty:
                        a["default"] = p.default
                    sch = _model_schema(ann)
                    if sch:
                        a["schema"] = sch
                    args.append(a)
                ret = {"type": _type_name(sig.return_annotation)}
                rsch = _model_schema(sig.return_annotation)
                if rsch:
                    ret["schema"] = rsch
                item["signature_detail"] = {"args": args, "returns": ret}
            # карта событий, если помечено
            topics = sorted(emits_map.get(qn, set()))
            if topics:
                item["emits"] = topics
            tools.append(item)

    events = []
    for topic, schema in event_payloads.items():
        events.append(
            {
                "kind": "event",
                "topic": topic,
                "payload": {"schema": schema},
            }
        )

    if level == "mini":
        lines = []
        for t in tools:
            lines.append({"k": "tool", "n": t["name"], "sig": t["signature"], "s": (t["summary"] or "")[:140], "ex": t.get("examples", [])[:1], "st": t["meta"]["stability"]})
        for e in events:
            lines.append({"k": "event", "topic": e["topic"], "p": list(e["payload"].get("schema", {}).keys())})
        return {"meta": meta, "items": lines}

    return {"meta": meta, "tools": tools, "events": events}
