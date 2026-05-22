from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

from adaos.domain.workspace_manifest import (
    parse_scenario_skill_bindings,
    parse_skill_activation_policy,
)


REGISTRY_FILE_NAME = "registry.json"
REGISTRY_FORMAT_VERSION = 1
RegistryKind = Literal["skills", "scenarios"]


def registry_pattern_set(patterns: Iterable[str]) -> list[str]:
    merged: list[str] = []
    if REGISTRY_FILE_NAME not in merged:
        merged.append(REGISTRY_FILE_NAME)
    for raw in patterns:
        try:
            value = str(raw).strip()
        except Exception:
            continue
        if value and value not in merged:
            merged.append(value)
    return merged


def workspace_registry_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / REGISTRY_FILE_NAME


def load_workspace_registry(workspace_root: Path, *, fallback_to_scan: bool = True) -> dict[str, Any]:
    path = workspace_registry_path(workspace_root)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        return _normalize_registry_payload(data)
    if fallback_to_scan:
        return rebuild_workspace_registry(workspace_root)
    return _normalize_registry_payload({})


def write_workspace_registry(workspace_root: Path, payload: dict[str, Any]) -> Path:
    path = workspace_registry_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_registry_payload(payload)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def rebuild_workspace_registry(workspace_root: Path) -> dict[str, Any]:
    root = Path(workspace_root)
    payload: dict[str, Any] = {
        "version": REGISTRY_FORMAT_VERSION,
        "updated_at": _now_iso(),
        "skills": [],
        "scenarios": [],
    }
    for kind in ("skills", "scenarios"):
        entries: list[dict[str, Any]] = []
        kind_root = root / kind
        if kind_root.exists():
            for child in sorted(kind_root.iterdir(), key=lambda item: item.name.lower()):
                if not child.is_dir():
                    continue
                entry = build_registry_entry(kind, child)
                if entry is not None:
                    entries.append(entry)
        payload[kind] = entries
    return _normalize_registry_payload(payload)


def upsert_workspace_registry_entry(
    workspace_root: Path,
    kind: RegistryKind,
    artifact_dir: Path,
    *,
    version: str | None = None,
    updated_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = load_workspace_registry(workspace_root, fallback_to_scan=True)
    entry = build_registry_entry(kind, artifact_dir)
    if entry is None:
        entry = _find_existing_registry_entry(payload, kind, Path(artifact_dir).name)
    if entry is None:
        raise FileNotFoundError(f"cannot build registry entry for {kind[:-1]} at {artifact_dir}")
    if version:
        entry["version"] = str(version)
    if updated_at:
        entry["updated_at"] = str(updated_at)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if value is None:
                continue
            entry[str(key)] = value
    items = list(payload.get(kind) or [])
    items = [item for item in items if isinstance(item, dict) and str(item.get("name") or "") != entry["name"]]
    items.append(entry)
    payload[kind] = _normalize_entries(kind, items)
    payload["updated_at"] = entry.get("updated_at") or _now_iso()
    write_workspace_registry(workspace_root, payload)
    return entry


def list_workspace_registry_entries(
    workspace_root: Path,
    *,
    kind: RegistryKind | None = None,
    name: str | None = None,
    fallback_to_scan: bool = True,
) -> list[dict[str, Any]]:
    payload = load_workspace_registry(workspace_root, fallback_to_scan=fallback_to_scan)
    kinds = (kind,) if kind else ("skills", "scenarios")
    results: list[dict[str, Any]] = []
    wanted_name = (name or "").strip().lower()
    for current_kind in kinds:
        for item in payload.get(current_kind) or []:
            if not isinstance(item, dict):
                continue
            artifact_name = str(item.get("name") or "")
            if wanted_name and artifact_name.lower() != wanted_name:
                continue
            results.append(dict(item))
    return results


def find_workspace_registry_entry(
    workspace_root: Path,
    *,
    kind: RegistryKind,
    name_or_id: str,
    fallback_to_scan: bool = True,
) -> dict[str, Any] | None:
    needle = str(name_or_id or "").strip().lower()
    if not needle:
        return None
    for item in list_workspace_registry_entries(
        workspace_root,
        kind=kind,
        fallback_to_scan=fallback_to_scan,
    ):
        name = str(item.get("name") or "").strip().lower()
        artifact_id = str(item.get("id") or "").strip().lower()
        if needle in {name, artifact_id}:
            return dict(item)
    return None


def workspace_registry_install_name(entry: dict[str, Any], *, kind: RegistryKind) -> str:
    install = entry.get("install")
    install_name = _clean_text(install.get("name")) if isinstance(install, dict) else ""
    name = install_name or _clean_text(entry.get("name"))
    if not name:
        path = _clean_text(entry.get("path"))
        prefix = f"{kind}/"
        if path.startswith(prefix):
            name = path[len(prefix) :].strip("/")
    return name or _clean_text(entry.get("id"))


def resolve_workspace_registry_install_name(
    workspace_root: Path,
    *,
    kind: RegistryKind,
    name_or_id: str,
    fallback_to_scan: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    entry = find_workspace_registry_entry(
        workspace_root,
        kind=kind,
        name_or_id=name_or_id,
        fallback_to_scan=fallback_to_scan,
    )
    if entry is None:
        return str(name_or_id or "").strip(), None
    install_name = workspace_registry_install_name(entry, kind=kind)
    return install_name or str(name_or_id or "").strip(), entry


def format_workspace_registry_not_found(
    workspace_root: Path,
    *,
    kind: RegistryKind,
    name_or_id: str,
    fallback_to_scan: bool = False,
    limit: int = 8,
) -> str:
    noun = "skill" if kind == "skills" else "scenario"
    requested = str(name_or_id or "").strip()
    entries = list_workspace_registry_entries(
        workspace_root,
        kind=kind,
        fallback_to_scan=fallback_to_scan,
    )
    needle = requested.lower()
    candidates: list[str] = []
    for item in entries:
        name = _clean_text(item.get("name"))
        artifact_id = _clean_text(item.get("id"))
        title = _clean_text(item.get("title"))
        haystack = " ".join(part for part in (name, artifact_id, title) if part).lower()
        if needle and needle in haystack:
            candidates.append(name or artifact_id)
    if not candidates:
        candidates = [
            workspace_registry_install_name(item, kind=kind)
            for item in entries[:limit]
        ]
    candidates = [item for item in dict.fromkeys(candidates) if item]
    suffix = ""
    if candidates:
        suffix = f" Available {kind} include: {', '.join(candidates[:limit])}."
    return (
        f"{noun} '{requested}' is not listed in workspace registry.json. "
        f"Install by the registry 'name' field, not by an arbitrary label.{suffix}"
    )


def build_registry_entry(kind: RegistryKind, artifact_dir: Path) -> dict[str, Any] | None:
    directory = Path(artifact_dir)
    manifest_path, manifest = _load_manifest(directory, kind)
    if manifest_path is None:
        return None

    artifact_name = directory.name
    manifest_id = _clean_text(manifest.get("id")) or artifact_name
    title = _clean_text(manifest.get("name"))
    description = _clean_text(manifest.get("description"))
    tags = _clean_tags(manifest.get("tags"))
    entry: dict[str, Any] = {
        "kind": kind[:-1],
        "id": manifest_id,
        "name": artifact_name,
        "version": _clean_text(manifest.get("version")) or "0.0.0",
        "updated_at": _clean_text(manifest.get("updated_at")) or _now_iso(),
        "path": f"{kind}/{artifact_name}",
        "manifest": f"{kind}/{artifact_name}/{manifest_path.name}",
        "source": {
            "path": f"{kind}/{artifact_name}",
            "manifest": f"{kind}/{artifact_name}/{manifest_path.name}",
        },
        "install": {
            "kind": kind[:-1],
            "name": artifact_name,
            "id": manifest_id,
        },
    }
    if title and title != artifact_name:
        entry["title"] = title
    if description:
        entry["description"] = description
    if tags:
        entry["tags"] = tags

    publisher = manifest.get("publisher")
    if isinstance(publisher, dict):
        publisher_entry = {str(key): value for key, value in publisher.items() if value is not None}
        if publisher_entry:
            entry["publisher"] = publisher_entry

    if kind == "skills":
        if manifest_id and manifest_id != artifact_name:
            entry["manifest_id"] = manifest_id
        manifest_entry = _clean_text(manifest.get("entry"))
        if manifest_entry:
            entry["entry"] = manifest_entry
        runtime = manifest.get("runtime")
        if isinstance(runtime, dict) and runtime:
            runtime_python = _clean_text(runtime.get("python"))
            if runtime_python:
                entry["runtime_python"] = runtime_python
        activation = parse_skill_activation_policy(manifest)
        if activation is not None:
            entry["activation"] = activation.to_dict()
        tools = manifest.get("tools")
        if isinstance(tools, list):
            entry["tools_count"] = len(tools)
    else:
        scenario_id = _clean_text(manifest.get("id"))
        if scenario_id and scenario_id != artifact_name:
            entry["manifest_id"] = scenario_id
        trigger = _clean_text(manifest.get("trigger"))
        if trigger:
            entry["trigger"] = trigger
        skills = parse_scenario_skill_bindings(manifest)
        skills_payload = skills.to_dict()
        if skills_payload:
            entry["skills"] = skills_payload
        io_meta = manifest.get("io")
        if isinstance(io_meta, dict):
            io_entry: dict[str, Any] = {}
            for key in ("input", "output"):
                value = io_meta.get(key)
                if isinstance(value, list):
                    io_entry[key] = [str(item) for item in value]
            if io_entry:
                entry["io"] = io_entry

    return entry


def _normalize_registry_payload(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    payload: dict[str, Any] = {
        "version": REGISTRY_FORMAT_VERSION,
        "updated_at": _clean_text(data.get("updated_at")) or _now_iso(),
        "skills": _normalize_entries("skills", data.get("skills")),
        "scenarios": _normalize_entries("scenarios", data.get("scenarios")),
    }
    return payload


def _normalize_entries(kind: RegistryKind, raw_entries: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list):
        return []
    merged: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("name")) or _clean_text(raw.get("id"))
        if not name:
            continue
        item = dict(raw)
        item["kind"] = kind[:-1]
        item["name"] = name
        merged[name] = item
    return [merged[key] for key in sorted(merged, key=str.lower)]


def _find_existing_registry_entry(
    payload: dict[str, Any],
    kind: RegistryKind,
    artifact_name: str,
) -> dict[str, Any] | None:
    token = _clean_text(artifact_name)
    if not token:
        return None
    for raw in list(payload.get(kind) or []):
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("name"))
        artifact_id = _clean_text(raw.get("id"))
        if token not in {name, artifact_id}:
            continue
        item = dict(raw)
        item["kind"] = kind[:-1]
        item["name"] = name or token
        item.setdefault("id", artifact_id or token)
        item.setdefault("path", f"{kind}/{token}")
        install = dict(item.get("install") or {}) if isinstance(item.get("install"), dict) else {}
        install.setdefault("kind", kind[:-1])
        install.setdefault("name", item["name"])
        install.setdefault("id", item.get("id") or token)
        item["install"] = install
        return item
    return None


def _load_manifest(directory: Path, kind: RegistryKind) -> tuple[Path | None, dict[str, Any]]:
    candidates = ("skill.yaml",) if kind == "skills" else ("scenario.yaml", "scenario.yml", "scenario.json")
    for candidate in candidates:
        path = directory / candidate
        if not path.exists():
            continue
        try:
            if path.suffix.lower() == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            return path, data
        return path, {}
    return None, {}


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _clean_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw = [str(item or "").strip() for item in value]
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not item:
            continue
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(item)
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "REGISTRY_FILE_NAME",
    "REGISTRY_FORMAT_VERSION",
    "build_registry_entry",
    "find_workspace_registry_entry",
    "list_workspace_registry_entries",
    "load_workspace_registry",
    "rebuild_workspace_registry",
    "registry_pattern_set",
    "upsert_workspace_registry_entry",
    "workspace_registry_path",
    "write_workspace_registry",
]
