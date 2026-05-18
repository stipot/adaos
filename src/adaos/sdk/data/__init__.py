"""Data-plane helpers exposed by the AdaOS SDK.

This module is intentionally import-light: it avoids eager imports that pull in
runtime services (scenario/yjs/etc.) so that service-layer modules can safely
depend on small SDK utilities without creating circular imports.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "BusNotAvailable",
    "emit",
    "on",
    "get_meta",
    "clear_current_skill",
    "set_current_skill",
    "get_current_skill",
    "publish",
    "tmp_path",
    "save_bytes",
    "open",
    "get",
    "put",
    "delete",
    "list",
    "read",
    "write",
    "profile_get_settings",
    "profile_update_settings",
    "ctx_subnet",
    "ctx_current_user",
    "ctx_selected_user",
    "ProjectionSlot",
    "ProjectionRuntime",
    "ProjectionContext",
    "ProjectionWriteResult",
    "ProjectionRefreshResult",
    "StreamReceiver",
    "StreamRuntime",
    "StreamPublishResult",
    "DirtyRouter",
    "SectionCache",
    "stable_payload_fingerprint",
    "get_projection_runtime",
    "set_projection_if_changed",
    "clear_projection_runtime_state",
    "I18n",
    "_",
    "skill_memory_get",
    "skill_memory_set",
    "skill_env_get",
    "skill_env_set",
    "entities_list",
    "entities_resolve_text",
    "get_tts_backend",
    "get_stt_backend",
    "get_audio_out_backend",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "BusNotAvailable": ("adaos.sdk.data.bus", "BusNotAvailable"),
    "emit": ("adaos.sdk.data.bus", "emit"),
    "on": ("adaos.sdk.data.bus", "on"),
    "get_meta": ("adaos.sdk.data.bus", "get_meta"),
    "clear_current_skill": ("adaos.sdk.data.context", "clear_current_skill"),
    "set_current_skill": ("adaos.sdk.data.context", "set_current_skill"),
    "get_current_skill": ("adaos.sdk.data.context", "get_current_skill"),
    "get_audio_out_backend": ("adaos.sdk.data.env", "get_audio_out_backend"),
    "get_stt_backend": ("adaos.sdk.data.env", "get_stt_backend"),
    "get_tts_backend": ("adaos.sdk.data.env", "get_tts_backend"),
    "publish": ("adaos.sdk.data.events", "publish"),
    "open": ("adaos.sdk.data.fs", "open"),
    "save_bytes": ("adaos.sdk.data.fs", "save_bytes"),
    "tmp_path": ("adaos.sdk.data.fs", "tmp_path"),
    "I18n": ("adaos.sdk.data.i18n", "I18n"),
    "_": ("adaos.sdk.data.i18n", "_"),
    "delete": ("adaos.sdk.data.memory", "delete"),
    "get": ("adaos.sdk.data.memory", "get"),
    "list": ("adaos.sdk.data.memory", "list"),
    "put": ("adaos.sdk.data.memory", "put"),
    "profile_get_settings": ("adaos.sdk.data.profile", "get_settings"),
    "profile_update_settings": ("adaos.sdk.data.profile", "update_settings"),
    "ctx_subnet": ("adaos.sdk.data.ctx", "subnet"),
    "ctx_current_user": ("adaos.sdk.data.ctx", "current_user"),
    "ctx_selected_user": ("adaos.sdk.data.ctx", "selected_user"),
    "ProjectionSlot": ("adaos.sdk.data.projections", "ProjectionSlot"),
    "ProjectionRuntime": ("adaos.sdk.data.projections", "ProjectionRuntime"),
    "ProjectionContext": ("adaos.sdk.data.projections", "ProjectionContext"),
    "ProjectionWriteResult": ("adaos.sdk.data.projections", "ProjectionWriteResult"),
    "ProjectionRefreshResult": ("adaos.sdk.data.projections", "ProjectionRefreshResult"),
    "StreamReceiver": ("adaos.sdk.data.projections", "StreamReceiver"),
    "StreamRuntime": ("adaos.sdk.data.projections", "StreamRuntime"),
    "StreamPublishResult": ("adaos.sdk.data.projections", "StreamPublishResult"),
    "DirtyRouter": ("adaos.sdk.data.projections", "DirtyRouter"),
    "SectionCache": ("adaos.sdk.data.projections", "SectionCache"),
    "stable_payload_fingerprint": ("adaos.sdk.data.projections", "stable_payload_fingerprint"),
    "get_projection_runtime": ("adaos.sdk.data.projections", "get_projection_runtime"),
    "set_projection_if_changed": ("adaos.sdk.data.projections", "set_projection_if_changed"),
    "clear_projection_runtime_state": ("adaos.sdk.data.projections", "clear_projection_runtime_state"),
    "read": ("adaos.sdk.data.secrets", "read"),
    "write": ("adaos.sdk.data.secrets", "write"),
    "skill_memory_get": ("adaos.sdk.data.skill_memory", "get"),
    "skill_memory_set": ("adaos.sdk.data.skill_memory", "set"),
    "skill_env_get": ("adaos.sdk.data.skill_env", "get_env"),
    "skill_env_set": ("adaos.sdk.data.skill_env", "set_env"),
    "entities_list": ("adaos.sdk.data.entities", "list_entities"),
    "entities_resolve_text": ("adaos.sdk.data.entities", "resolve_text"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    mod, attr = target
    return getattr(import_module(mod), attr)
