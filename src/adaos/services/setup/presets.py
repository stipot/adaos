from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InstallPreset:
    name: str
    scenarios: tuple[str, ...]
    skills: tuple[str, ...]


DEFAULT_PRESET = InstallPreset(
    name="default",
    scenarios=(
        "web_desktop",
        "prompt_engineer_scenario",
        "infrascope",
        "taiga_ui_demo_scenario",
    ),
    skills=(
        "infrastate_skill",
        "weather_skill",
        "web_desktop_skill",
        "prompt_engineer_skill",
        "adaos_connect",
        "browsers_skill",
        "infra_access_skill",
        "mediaserver",
        "rasa_nlu_service_skill",
        "subnet_env",
        "voice_chat_skill",
    ),
)


def get_preset(name: str | None) -> InstallPreset:
    normalized = (name or "default").strip().lower()
    if normalized in {"default", "base"}:
        return DEFAULT_PRESET
    raise ValueError(f"unknown preset: {name}")
