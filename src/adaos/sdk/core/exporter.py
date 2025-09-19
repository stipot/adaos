"""Export tool metadata for discovery by LLMs and other clients."""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .decorators import emits_map, event_payloads, tools_meta, tools_registry

_ALLOWED_TOOL_PREFIXES: Tuple[str, ...] = ("manage.", "skills.", "scenarios.", "resources.")
_DISCOVERY_PACKAGES: Tuple[str, ...] = ("adaos.sdk.manage", "adaos.sdk.data")


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # pragma: no cover - git not available
        return "unknown"


def _preload_modules() -> None:
    env_override = os.getenv("ADAOS_SDK_EXPORT_MODULES")
    if env_override:
        modules = [m.strip() for m in env_override.split(",") if m.strip()]
    else:
        modules = list(_DISCOVERY_PACKAGES)
    for mod_name in modules:
        try:
            module = importlib.import_module(mod_name)
        except Exception:  # pragma: no cover - import errors should not break export
            continue
        path = getattr(module, "__path__", None)
        if not path:
            continue
        for finder in pkgutil.walk_packages(path, prefix=f"{module.__name__}."):
            try:
                importlib.import_module(finder.name)
            except Exception:  # pragma: no cover - skip faulty modules silently
                continue


def _filter_tools() -> List[Tuple[str, str, Any]]:
    items: List[Tuple[str, str, Any]] = []
    for module_name, mapping in tools_registry.items():
        if not module_name.startswith(_DISCOVERY_PACKAGES):
            continue
        for public_name, fn in mapping.items():
            if not public_name.startswith(_ALLOWED_TOOL_PREFIXES):
                continue
            items.append((public_name, module_name, fn))
    return items


def _doc_summary(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0][:200]


def export(level: str = "std") -> Dict[str, Any]:
    """Return metadata about all exported tools and events."""

    _preload_modules()

    seen_names: set[str] = set()
    tools: List[Dict[str, Any]] = []
    for public_name, module_name, fn in sorted(_filter_tools(), key=lambda it: it[0]):
        if public_name in seen_names:
            raise RuntimeError(f"duplicate tool name detected: {public_name}")
        seen_names.add(public_name)
        qn = f"{fn.__module__}.{fn.__name__}"
        meta = tools_meta.get(qn, {})
        item: Dict[str, Any] = {
            "kind": "tool",
            "name": public_name,
            "module": module_name,
            "qualname": qn,
            "summary": meta.get("summary") or _doc_summary(fn.__doc__),
            "meta": {
                "stability": meta.get("stability", "experimental"),
                "idempotent": meta.get("idempotent"),
                "side_effects": meta.get("side_effects"),
                "since": meta.get("since"),
                "version": meta.get("version"),
            },
            "examples": meta.get("examples", []),
        }
        if level in ("std", "rich"):
            sig = inspect.signature(fn)
            args = []
            for name, param in sig.parameters.items():
                entry: Dict[str, Any] = {"name": name, "annotation": str(param.annotation)}
                if param.default is not inspect._empty:
                    entry["default"] = param.default
                args.append(entry)
            returns: Dict[str, Any] = {"annotation": str(sig.return_annotation)}
            item["signature_detail"] = {"args": args, "returns": returns}
        if meta.get("input_schema"):
            item["input_schema"] = meta["input_schema"]
        if meta.get("output_schema"):
            item["output_schema"] = meta["output_schema"]
        topics = sorted(emits_map.get(qn, set()))
        if topics:
            item["emits"] = topics
        tools.append(item)

    events = [
        {
            "kind": "event",
            "topic": topic,
            "payload": {"schema": schema},
        }
        for topic, schema in sorted(event_payloads.items())
    ]

    meta = {
        "generated_at": _iso_now(),
        "git_sha": _git_sha(),
        "py": f"{sys.version_info.major}.{sys.version_info.minor}",
    }

    if level == "mini":
        items = []
        for tool in tools:
            items.append(
                {
                    "k": "tool",
                    "n": tool["name"],
                    "s": (tool.get("summary") or "")[:140],
                    "st": tool["meta"].get("stability"),
                }
            )
        for event in events:
            items.append({"k": "event", "topic": event["topic"]})
        return {"meta": meta, "items": items}

    return {"meta": meta, "tools": tools, "events": events}


__all__ = ["export"]
