"""Registry of lightweight service ports used by the scenario runner."""

from __future__ import annotations

from typing import Any, Dict

from adaos.sdk.data import memory


def _compose_greeting(*, name: Any, default: Any, weather: Any) -> str:
    name_str = (str(name).strip() if name is not None else "")
    default_str = (str(default).strip() if default is not None else "")
    weather_str = (str(weather).strip() if weather is not None else "")

    if name_str:
        greeting = f"Привет, {name_str}!"
    else:
        greeting = default_str

    parts = [part for part in (greeting, weather_str) if part]
    return "\n".join(parts).strip()


def call(route: str, args: Dict[str, Any] | None = None) -> Any:
    args = args or {}

    if route == "io.console.print":
        from .io_console import print as fn

        return fn(args.get("text"))

    if route == "io.voice.tts.speak":
        from .io_voice_mock import tts_speak as fn

        return fn(args.get("text"))

    if route == "io.voice.stt.listen":
        from .io_voice_mock import stt_listen as fn

        return fn(args.get("timeout", "20s"))

    if route == "profile.get_name":
        return memory.get("user.name")

    if route == "profile.set_name":
        raw = args.get("name")
        value = str(raw).strip() if raw is not None else ""
        if not value:
            memory.delete("user.name")
            return None
        memory.put("user.name", value)
        return value

    if route == "skills.call_fs":
        from .skills_loader_fs import call_fs

        return call_fs(args)

    if route == "runtime.compose_greeting":
        return _compose_greeting(
            name=args.get("name"),
            default=args.get("default_greeting"),
            weather=args.get("weather"),
        )

    raise RuntimeError(f"unknown route: {route}")


__all__ = ["call"]
