from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _normalize_y_path(value: Any) -> str | None:
    token = str(value or "").strip().replace("\\", "/")
    if token.startswith("y:"):
        token = token[2:].strip()
    token = token.strip("/")
    if not token.startswith("data/"):
        return None
    return token


def _path_root(path: str) -> str:
    parts = [part for part in str(path or "").split("/") if part]
    if len(parts) >= 2 and parts[0] == "data":
        return "/".join(parts[:2])
    return str(path or "").strip("/")


def _iter_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _iter_mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_mappings(item)


def _extract_manifest_projection_paths(manifest: Mapping[str, Any]) -> list[str]:
    paths: set[str] = set()
    entries = manifest.get("data_projections")
    if not isinstance(entries, list):
        return []
    for entry in entries:
        data = _mapping(entry)
        targets = data.get("targets")
        if not isinstance(targets, list):
            continue
        for target in targets:
            target_data = _mapping(target)
            if str(target_data.get("backend") or "").strip().lower() != "yjs":
                continue
            path = _normalize_y_path(target_data.get("path"))
            if path:
                paths.add(path)
    return sorted(paths)


def _extract_webui_y_paths(webui: Mapping[str, Any]) -> list[str]:
    paths: set[str] = set()
    for data in _iter_mappings(webui):
        source = _normalize_y_path(data.get("source"))
        if source:
            paths.add(source)
        data_source = _mapping(data.get("dataSource"))
        if str(data_source.get("kind") or "").strip().lower() == "y":
            path = _normalize_y_path(data_source.get("path"))
            if path:
                paths.add(path)
    return sorted(paths)


def _extract_ydoc_default_paths(webui: Mapping[str, Any]) -> list[str]:
    defaults = _mapping(webui.get("ydoc_defaults"))
    return sorted(path for value in defaults for path in [_normalize_y_path(value)] if path)


def _extract_stream_receivers(webui: Mapping[str, Any]) -> list[str]:
    receivers: set[str] = set()
    webio_receivers = _mapping(_mapping(webui.get("webio")).get("receivers"))
    receivers.update(str(key) for key in webio_receivers if str(key or "").strip())
    for data in _iter_mappings(webui):
        data_source = _mapping(data.get("dataSource"))
        if str(data_source.get("kind") or "").strip().lower() == "stream":
            receiver = str(data_source.get("receiver") or "").strip()
            if receiver:
                receivers.add(receiver)
    return sorted(receivers)


def _count_registry_modals(webui: Mapping[str, Any]) -> int:
    registry = _mapping(webui.get("registry"))
    modals = _mapping(registry.get("modals"))
    return len(modals)


def _handler_text(skill_dir: Path) -> str:
    candidates = [skill_dir / "handlers" / "main.py", skill_dir / "handler.py"]
    chunks: list[str] = []
    for path in candidates:
        if path.exists():
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                pass
    return "\n".join(chunks)


def _shared_bridge(skill_id: str, handler_text: str) -> str | None:
    if skill_id in {"infrascope_skill", "infrastate_skill"}:
        return "status-card-adapter"
    if "publish_status" in handler_text or "adaos.sdk.status" in handler_text:
        return "sdk-status"
    return None


def _root_summaries(
    *,
    manifest_paths: list[str],
    webui_paths: list[str],
    default_paths: list[str],
) -> list[dict[str, Any]]:
    all_paths = sorted({*manifest_paths, *webui_paths, *default_paths})
    by_root: dict[str, set[str]] = {}
    for path in all_paths:
        by_root.setdefault(_path_root(path), set()).add(path)
    roots: list[dict[str, Any]] = []
    for root, paths_set in sorted(by_root.items()):
        paths = sorted(paths_set)
        root_is_direct = root in paths
        if root_is_direct:
            shape = "monolithic-yjs-root"
        elif len(paths) == 1:
            shape = "single-yjs-slot"
        else:
            shape = "sectioned-yjs-root"
        roots.append(
            {
                "root": root,
                "shape": shape,
                "path_total": len(paths),
                "paths": paths,
                "manifest_paths": [path for path in paths if path in manifest_paths],
                "webui_paths": [path for path in paths if path in webui_paths],
                "ydoc_default_paths": [path for path in paths if path in default_paths],
            }
        )
    return roots


def _migration_risk(*, roots: list[Mapping[str, Any]], stream_receivers: list[str], shared_bridge: str | None) -> str:
    has_monolithic = any(str(root.get("shape") or "") == "monolithic-yjs-root" for root in roots)
    if not has_monolithic:
        return "low"
    if shared_bridge:
        return "medium"
    if stream_receivers:
        return "medium"
    return "high"


def _root_shape_total(item: Mapping[str, Any], shape: str) -> int:
    return sum(1 for root in item.get("roots", []) if _mapping(root).get("shape") == shape)


def _migration_metric_summary(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    risk_counts = {
        level: sum(1 for item in items if item.get("risk") == level)
        for level in ("high", "medium", "low")
    }
    monolithic_root_total = sum(_root_shape_total(item, "monolithic-yjs-root") for item in items)
    sectioned_yjs_root_total = sum(_root_shape_total(item, "sectioned-yjs-root") for item in items)
    single_yjs_slot_total = sum(_root_shape_total(item, "single-yjs-slot") for item in items)
    stream_receiver_total = sum(int(item.get("stream_receiver_total") or 0) for item in items)
    shared_bridge_total = sum(1 for item in items if item.get("shared_bridge"))
    browser_surface_total = sum(
        int(item.get("app_total") or 0) + int(item.get("widget_total") or 0) + int(item.get("modal_total") or 0)
        for item in items
    )
    modern_surface_total = (
        sectioned_yjs_root_total
        + single_yjs_slot_total
        + stream_receiver_total
        + shared_bridge_total
    )
    observed_surface_total = monolithic_root_total + modern_surface_total
    migration_readiness_ratio = (
        round(modern_surface_total / observed_surface_total, 4)
        if observed_surface_total
        else 1.0
    )
    monolith_exposure_ratio = (
        round(monolithic_root_total / observed_surface_total, 4)
        if observed_surface_total
        else 0.0
    )
    risk_weights = {"high": 3, "medium": 2, "low": 1}
    legacy_pressure_score = sum(
        _root_shape_total(item, "monolithic-yjs-root") * risk_weights.get(str(item.get("risk")), 1)
        for item in items
    )
    modern_coverage_score = modern_surface_total + shared_bridge_total
    return {
        "skill_total": len(items),
        "browser_facing_skill_total": sum(1 for item in items if item.get("browser_facing")),
        "browser_surface_total": browser_surface_total,
        "monolithic_candidate_total": sum(1 for item in items if item.get("monolithic_candidate")),
        "monolithic_root_total": monolithic_root_total,
        "sectioned_yjs_root_total": sectioned_yjs_root_total,
        "single_yjs_slot_total": single_yjs_slot_total,
        "stream_receiver_total": stream_receiver_total,
        "shared_bridge_total": shared_bridge_total,
        "modern_surface_total": modern_surface_total,
        "observed_surface_total": observed_surface_total,
        "migration_readiness_ratio": migration_readiness_ratio,
        "monolith_exposure_ratio": monolith_exposure_ratio,
        "legacy_pressure_score": legacy_pressure_score,
        "modern_coverage_score": modern_coverage_score,
        "risk_counts": risk_counts,
    }


def _top_monolithic_candidates(items: list[Mapping[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    risk_order = {"high": 0, "medium": 1, "low": 2}
    candidates = [
        {
            "skill_id": str(item.get("skill_id") or ""),
            "risk": str(item.get("risk") or "unknown"),
            "monolithic_root_total": int(item.get("monolithic_root_total") or 0),
            "shared_bridge": item.get("shared_bridge"),
            "stream_receiver_total": int(item.get("stream_receiver_total") or 0),
        }
        for item in items
        if item.get("monolithic_candidate")
    ]
    return sorted(
        candidates,
        key=lambda item: (
            risk_order.get(str(item.get("risk")), 99),
            -int(item.get("monolithic_root_total") or 0),
            str(item.get("skill_id") or ""),
        ),
    )[: max(0, int(limit))]


def _metric_definitions() -> list[dict[str, Any]]:
    return [
        {
            "metric": "monolith_exposure_ratio",
            "direction": "lower_is_better",
            "formula": "monolithic_root_total / observed_surface_total",
            "meaning": "Share of browser-facing projection surfaces that still depend on direct monolithic Yjs roots.",
        },
        {
            "metric": "migration_readiness_ratio",
            "direction": "higher_is_better",
            "formula": "modern_surface_total / observed_surface_total",
            "meaning": "Share of projection surfaces already represented by sectioned/single-slot Yjs, stream receivers, or shared bridges.",
        },
        {
            "metric": "legacy_pressure_score",
            "direction": "lower_is_better",
            "formula": "sum(monolithic_roots * risk_weight)",
            "meaning": "Weighted backlog of monolithic publishers; high-risk skills count more than transitional skills.",
        },
    ]


def _iter_skill_dirs(skills_root: Path) -> Iterable[Path]:
    if not skills_root.exists():
        return []
    return (
        child
        for child in sorted(skills_root.iterdir(), key=lambda item: item.name)
        if child.is_dir()
        and not child.name.startswith(".")
        and ((child / "skill.yaml").exists() or (child / "webui.json").exists())
    )


def inspect_skill_projection_migration(skill_dir: Path) -> dict[str, Any]:
    manifest = _read_yaml(skill_dir / "skill.yaml")
    webui = _read_json(skill_dir / "webui.json")
    skill_id = str(manifest.get("name") or manifest.get("id") or skill_dir.name).strip() or skill_dir.name
    manifest_paths = _extract_manifest_projection_paths(manifest)
    webui_paths = _extract_webui_y_paths(webui)
    default_paths = _extract_ydoc_default_paths(webui)
    stream_receivers = _extract_stream_receivers(webui)
    roots = _root_summaries(
        manifest_paths=manifest_paths,
        webui_paths=webui_paths,
        default_paths=default_paths,
    )
    handler = _handler_text(skill_dir)
    shared_bridge = _shared_bridge(skill_id, handler)
    apps = webui.get("apps") if isinstance(webui.get("apps"), list) else []
    widgets = webui.get("widgets") if isinstance(webui.get("widgets"), list) else []
    browser_facing = bool(apps or widgets or _count_registry_modals(webui) or stream_receivers or webui_paths)
    monolithic_roots = [root for root in roots if root.get("shape") == "monolithic-yjs-root"]
    risk = _migration_risk(roots=roots, stream_receivers=stream_receivers, shared_bridge=shared_bridge)
    return {
        "skill_id": skill_id,
        "version": manifest.get("version"),
        "path": str(skill_dir),
        "browser_facing": browser_facing,
        "shared_bridge": shared_bridge,
        "risk": risk,
        "monolithic_candidate": bool(monolithic_roots),
        "monolithic_root_total": len(monolithic_roots),
        "data_projection_total": len(manifest_paths),
        "webui_y_path_total": len(webui_paths),
        "ydoc_default_total": len(default_paths),
        "stream_receiver_total": len(stream_receivers),
        "app_total": len(apps),
        "widget_total": len(widgets),
        "modal_total": _count_registry_modals(webui),
        "manifest_yjs_paths": manifest_paths,
        "webui_y_paths": webui_paths,
        "ydoc_default_paths": default_paths,
        "stream_receivers": stream_receivers,
        "roots": roots,
    }


def projection_migration_monolith_inventory(
    *,
    skills_root: str | Path,
    include_non_browser: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    root = Path(skills_root)
    items = [
        inspect_skill_projection_migration(skill_dir)
        for skill_dir in _iter_skill_dirs(root)
    ]
    if not include_non_browser:
        items = [item for item in items if bool(item.get("browser_facing"))]
    risk_counts = {
        level: sum(1 for item in items if item.get("risk") == level)
        for level in ("high", "medium", "low")
    }
    shape_counts: dict[str, int] = {}
    for item in items:
        for root_item in item.get("roots", []):
            shape = str(_mapping(root_item).get("shape") or "unknown")
            shape_counts[shape] = shape_counts.get(shape, 0) + 1
    risk_order = {"high": 0, "medium": 1, "low": 2}
    return {
        "ok": True,
        "skills_root": str(root),
        "include_non_browser": bool(include_non_browser),
        "scanned_total": len(items),
        "browser_facing_total": sum(1 for item in items if item.get("browser_facing")),
        "monolithic_candidate_total": sum(1 for item in items if item.get("monolithic_candidate")),
        "shared_bridge_total": sum(1 for item in items if item.get("shared_bridge")),
        "risk_counts": risk_counts,
        "shape_counts": dict(sorted(shape_counts.items())),
        "items": sorted(
            items,
            key=lambda item: (risk_order.get(str(item.get("risk")), 99), str(item.get("skill_id"))),
        ),
        "updated_at": float(now if now is not None else time.time()),
    }


def projection_migration_metrics(
    *,
    skills_root: str | Path,
    include_non_browser: bool = False,
    top_limit: int = 5,
    now: float | None = None,
) -> dict[str, Any]:
    inventory = projection_migration_monolith_inventory(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        now=now,
    )
    items = [item for item in inventory.get("items", []) if isinstance(item, Mapping)]
    metrics = _migration_metric_summary(items)
    return {
        "ok": True,
        "skills_root": inventory["skills_root"],
        "include_non_browser": bool(include_non_browser),
        "metrics": metrics,
        "metric_definitions": _metric_definitions(),
        "top_monolithic_candidates": _top_monolithic_candidates(items, limit=top_limit),
        "control_examples": [
            {
                "id": "monolith_inventory",
                "endpoint": "/api/node/projection-migration/monolith-inventory",
                "checks": ["monolithic_candidate_total", "risk_counts", "items[].roots[].shape"],
            },
            {
                "id": "migration_metrics",
                "endpoint": "/api/node/projection-migration/metrics",
                "checks": ["monolith_exposure_ratio", "migration_readiness_ratio", "legacy_pressure_score"],
            },
            {
                "id": "runtime_write_suppression",
                "source": "ProjectionRuntime.diagnostics_snapshot()",
                "checks": ["applied_total", "skipped_unchanged_total", "dirty_dropped_total"],
            },
        ],
        "updated_at": inventory["updated_at"],
    }


__all__ = [
    "inspect_skill_projection_migration",
    "projection_migration_metrics",
    "projection_migration_monolith_inventory",
]
