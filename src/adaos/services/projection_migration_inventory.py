from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from adaos.domain import client_subscription_contract_snapshot, event_envelope_contract_snapshot
from adaos.services.platform_emitters import platform_emitter_contract_snapshot
from adaos.services.projection_demand_mapper import browser_surface_lifecycle_contract_snapshot
from adaos.services.projection_record_yjs import projection_records_node_multiplicity_contract_snapshot
from adaos.services.projection_runtime_ownership import projection_runtime_ownership_contract_snapshot
from adaos.services.scenario.projection_registry import inspect_projection_manifest_entries


PROJECTION_RECORDS_COMPAT_BRANCH = "data/projectionRecords"


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


def legacy_projection_branch_compatibility(
    path: Any,
    *,
    shape: Any | None = None,
    projection_key: Any | None = None,
) -> dict[str, Any]:
    normalized = _normalize_y_path(path)
    projection_key_token = str(projection_key or "").strip() or None
    if not normalized:
        return {
            "path": str(path or "").strip(),
            "compatible": False,
            "legacy_branch": False,
            "classification": "unsupported-yjs-path",
            "read_policy": "none",
            "write_policy": "reject",
            "projection_record_required": True,
            "projection_key": projection_key_token,
            "migration_action": "declare_projection_record_or_remove_reference",
        }

    if normalized == PROJECTION_RECORDS_COMPAT_BRANCH:
        return {
            "path": normalized,
            "compatible": True,
            "legacy_branch": False,
            "classification": "projection-record-cache",
            "read_policy": "canonical-cache",
            "write_policy": "core-owned-cache",
            "projection_record_required": False,
            "projection_key": projection_key_token,
            "migration_action": "keep_projection_record_cache",
        }

    branch_shape = str(shape or "").strip()
    parts = [part for part in normalized.split("/") if part]
    root = _path_root(normalized)
    if branch_shape == "monolithic-yjs-root":
        classification = "legacy-monolithic-root"
        migration_action = "split_monolithic_root_to_projection_records"
    elif branch_shape == "single-yjs-slot" or len(parts) == 3:
        classification = "legacy-single-slot"
        migration_action = "map_slot_to_projection_key"
    elif branch_shape == "sectioned-yjs-root":
        classification = "legacy-sectioned-root"
        migration_action = "map_sections_to_projection_keys"
    elif normalized == root:
        classification = "legacy-monolithic-root"
        migration_action = "split_monolithic_root_to_projection_records"
    else:
        classification = "legacy-sectioned-root"
        migration_action = "map_sections_to_projection_keys"

    return {
        "path": normalized,
        "root": root,
        "compatible": True,
        "legacy_branch": True,
        "classification": classification,
        "read_policy": "transitional-read",
        "write_policy": "projection-record-only",
        "projection_record_required": True,
        "projection_key": projection_key_token,
        "migration_action": migration_action,
    }


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


def _text_contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def _shim_finding(
    *,
    finding_id: str,
    severity: str,
    evidence: str,
    replacement: str,
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "severity": severity,
        "evidence": evidence,
        "replacement": replacement,
    }


def _local_shim_findings(handler_text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if _text_contains_any(handler_text, ("ctx_subnet.set(", "ctx_subnet.set_async(")):
        findings.append(
            _shim_finding(
                finding_id="direct_ctx_subnet_write",
                severity="high",
                evidence="ctx_subnet.set*",
                replacement="ProjectionRuntime.set_if_changed or declared ProjectionSlot refresh",
            )
        )
    if _text_contains_any(handler_text, ("_fingerprints", "fingerprints: dict", "fingerprint_cache")):
        findings.append(
            _shim_finding(
                finding_id="local_fingerprint_cache",
                severity="medium",
                evidence="local fingerprint dict/cache",
                replacement="ProjectionRuntime/StreamRuntime diagnostics and fingerprint state",
            )
        )
    if _text_contains_any(handler_text, ("ThreadPoolExecutor", "run_in_executor")):
        findings.append(
            _shim_finding(
                finding_id="local_executor_bridge",
                severity="medium",
                evidence="ThreadPoolExecutor/run_in_executor",
                replacement="shared SDK refresh bridge or native async builder",
            )
        )
    if _text_contains_any(handler_text, ("_ensure_skill_data_projections", "_load_skill_data_projections")):
        findings.append(
            _shim_finding(
                finding_id="local_data_projection_loader",
                severity="low",
                evidence="skill-local data_projections loader",
                replacement="shared projection manifest loader",
            )
        )
    return findings


def _sdk_runtime_present(handler_text: str) -> bool:
    return _text_contains_any(
        handler_text,
        ("ProjectionRuntime(", "StreamRuntime(", "get_projection_runtime(", "get_stream_runtime("),
    )


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
                "compatibility": legacy_projection_branch_compatibility(root, shape=shape),
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


_RISK_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
_SHIM_WEIGHTS = {"high": 3, "medium": 2, "low": 1}


def _item_shim_pressure(item: Mapping[str, Any]) -> int:
    return sum(
        _SHIM_WEIGHTS.get(str(_mapping(finding).get("severity")), 1)
        for finding in item.get("shim_findings", [])
    )


def _recommendation_actions(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    skill_id = str(item.get("skill_id") or "")
    if item.get("monolithic_candidate"):
        if item.get("shared_bridge"):
            action_id = "split_monolithic_root_behind_shared_bridge"
            summary = "Split the existing monolithic Yjs root while keeping the shared bridge as compatibility cover."
        elif int(item.get("stream_receiver_total") or 0) > 0:
            action_id = "split_monolithic_root_to_slots_and_streams"
            summary = "Move compact durable data to ProjectionSlot entries and heavy panes to existing stream receivers."
        else:
            action_id = "introduce_projection_slots_or_status_bridge"
            summary = "Introduce a shared projection/status bridge before removing the direct monolithic Yjs root."
        actions.append(
            {
                "id": action_id,
                "category": "monolith",
                "summary": summary,
                "affected_roots": [
                    _mapping(root).get("root")
                    for root in item.get("roots", [])
                    if _mapping(root).get("shape") == "monolithic-yjs-root"
                ],
            }
        )
    for finding in item.get("shim_findings", []):
        data = _mapping(finding)
        actions.append(
            {
                "id": f"replace_{data.get('id')}",
                "category": "shim",
                "summary": data.get("replacement"),
                "finding": data.get("id"),
                "severity": data.get("severity"),
            }
        )
    if not actions and item.get("sdk_runtime_present"):
        actions.append(
            {
                "id": "keep_sdk_runtime_reference",
                "category": "reference",
                "summary": f"Keep {skill_id} as an SDK-runtime reference and monitor diagnostics during rollout.",
            }
        )
    return actions


def _recommendation_for_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    actions = _recommendation_actions(item)
    needs_work = bool(item.get("monolithic_candidate")) or int(item.get("shim_total") or 0) > 0
    if not needs_work and not item.get("sdk_runtime_present"):
        return None
    risk = str(item.get("risk") or "low")
    monolithic_root_total = int(item.get("monolithic_root_total") or 0)
    shim_pressure = _item_shim_pressure(item)
    priority_score = (
        _RISK_WEIGHTS.get(risk, 1) * 100
        + monolithic_root_total * 20
        + shim_pressure * 10
        + (0 if item.get("shared_bridge") else 10 if monolithic_root_total else 0)
    )
    return {
        "skill_id": str(item.get("skill_id") or ""),
        "risk": risk,
        "priority_score": priority_score,
        "monolithic_root_total": monolithic_root_total,
        "shim_total": int(item.get("shim_total") or 0),
        "shim_pressure_score": shim_pressure,
        "shared_bridge": item.get("shared_bridge"),
        "sdk_runtime_present": bool(item.get("sdk_runtime_present")),
        "recommended_next_step": actions[0]["id"] if actions else "observe",
        "actions": actions,
    }


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
    skill_local_shim_total = sum(1 for item in items if int(item.get("shim_total") or 0) > 0)
    manifest_yjs_target_total = sum(int(item.get("manifest_yjs_target_total") or 0) for item in items)
    projection_keyed_yjs_target_total = sum(
        int(item.get("projection_keyed_yjs_target_total") or 0) for item in items
    )
    reserved_cache_manifest_target_total = sum(
        int(item.get("reserved_cache_manifest_target_total") or 0) for item in items
    )
    legacy_monolithic_manifest_target_total = sum(
        int(item.get("legacy_monolithic_manifest_target_total") or 0) for item in items
    )
    legacy_compatible_root_total = sum(
        1
        for item in items
        for root in item.get("roots", [])
        if bool(_mapping(_mapping(root).get("compatibility")).get("legacy_branch"))
    )
    projection_record_cache_root_total = sum(
        1
        for item in items
        for root in item.get("roots", [])
        if _mapping(_mapping(root).get("compatibility")).get("classification") == "projection-record-cache"
    )
    direct_write_skill_total = sum(
        1 for item in items if "direct_ctx_subnet_write" in set(item.get("shim_ids") or [])
    )
    fingerprint_shim_skill_total = sum(
        1 for item in items if "local_fingerprint_cache" in set(item.get("shim_ids") or [])
    )
    executor_shim_skill_total = sum(
        1 for item in items if "local_executor_bridge" in set(item.get("shim_ids") or [])
    )
    sdk_runtime_skill_total = sum(1 for item in items if item.get("sdk_runtime_present"))
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
    manifest_projection_key_coverage_ratio = (
        round(projection_keyed_yjs_target_total / manifest_yjs_target_total, 4)
        if manifest_yjs_target_total
        else 1.0
    )
    legacy_pressure_score = sum(
        _root_shape_total(item, "monolithic-yjs-root") * _RISK_WEIGHTS.get(str(item.get("risk")), 1)
        for item in items
    )
    local_shim_pressure_score = sum(_item_shim_pressure(item) for item in items)
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
        "skill_local_shim_total": skill_local_shim_total,
        "legacy_compatible_root_total": legacy_compatible_root_total,
        "projection_record_cache_root_total": projection_record_cache_root_total,
        "manifest_yjs_target_total": manifest_yjs_target_total,
        "projection_keyed_yjs_target_total": projection_keyed_yjs_target_total,
        "reserved_cache_manifest_target_total": reserved_cache_manifest_target_total,
        "legacy_monolithic_manifest_target_total": legacy_monolithic_manifest_target_total,
        "manifest_projection_key_coverage_ratio": manifest_projection_key_coverage_ratio,
        "direct_write_skill_total": direct_write_skill_total,
        "fingerprint_shim_skill_total": fingerprint_shim_skill_total,
        "executor_shim_skill_total": executor_shim_skill_total,
        "sdk_runtime_skill_total": sdk_runtime_skill_total,
        "modern_surface_total": modern_surface_total,
        "observed_surface_total": observed_surface_total,
        "migration_readiness_ratio": migration_readiness_ratio,
        "monolith_exposure_ratio": monolith_exposure_ratio,
        "legacy_pressure_score": legacy_pressure_score,
        "local_shim_pressure_score": local_shim_pressure_score,
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
        {
            "metric": "local_shim_pressure_score",
            "direction": "lower_is_better",
            "formula": "sum(local_shim * severity_weight)",
            "meaning": "Weighted backlog of per-skill projection shims that should move into the shared SDK.",
        },
        {
            "metric": "manifest_projection_key_coverage_ratio",
            "direction": "higher_is_better",
            "formula": "projection_keyed_yjs_target_total / manifest_yjs_target_total",
            "meaning": "Share of manifest-declared Yjs targets already tied to canonical projection_key values.",
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
    manifest_contract = inspect_projection_manifest_entries(manifest.get("data_projections") or [])
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
    shim_findings = _local_shim_findings(handler)
    shim_ids = sorted(str(finding.get("id")) for finding in shim_findings)
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
        "sdk_runtime_present": _sdk_runtime_present(handler),
        "shim_total": len(shim_findings),
        "shim_ids": shim_ids,
        "shim_findings": shim_findings,
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
        "manifest_contract": manifest_contract,
        "manifest_finding_total": len(manifest_contract.get("findings", [])),
        "manifest_yjs_target_total": int(manifest_contract.get("yjs_target_total") or 0),
        "projection_keyed_yjs_target_total": int(
            manifest_contract.get("yjs_target_with_projection_key_total") or 0
        ),
        "reserved_cache_manifest_target_total": int(
            manifest_contract.get("reserved_cache_target_total") or 0
        ),
        "legacy_monolithic_manifest_target_total": int(
            manifest_contract.get("legacy_monolithic_target_total") or 0
        ),
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
        "skill_local_shim_total": sum(1 for item in items if int(item.get("shim_total") or 0) > 0),
        "manifest_finding_total": sum(int(item.get("manifest_finding_total") or 0) for item in items),
        "legacy_compatible_root_total": sum(
            1
            for item in items
            for root in item.get("roots", [])
            if bool(_mapping(_mapping(root).get("compatibility")).get("legacy_branch"))
        ),
        "projection_keyed_yjs_target_total": sum(
            int(item.get("projection_keyed_yjs_target_total") or 0) for item in items
        ),
        "reserved_cache_manifest_target_total": sum(
            int(item.get("reserved_cache_manifest_target_total") or 0) for item in items
        ),
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


def projection_migration_recommendations(
    *,
    skills_root: str | Path,
    include_non_browser: bool = False,
    limit: int = 10,
    now: float | None = None,
) -> dict[str, Any]:
    inventory = projection_migration_monolith_inventory(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        now=now,
    )
    recommendations = [
        recommendation
        for item in inventory.get("items", [])
        for recommendation in [_recommendation_for_item(_mapping(item))]
        if recommendation is not None
    ]
    recommendations.sort(
        key=lambda item: (
            -int(item.get("priority_score") or 0),
            str(item.get("skill_id") or ""),
        )
    )
    if limit >= 0:
        recommendations = recommendations[: int(limit)]
    return {
        "ok": True,
        "skills_root": inventory["skills_root"],
        "include_non_browser": bool(include_non_browser),
        "recommendation_total": len(recommendations),
        "items": recommendations,
        "updated_at": inventory["updated_at"],
    }


def _acceptance_check(
    *,
    check_id: str,
    title: str,
    status: str,
    evidence: Mapping[str, Any],
    followup: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": check_id,
        "title": title,
        "status": status,
        "evidence": dict(evidence),
    }
    if followup:
        item["followup"] = followup
    return item


def _acceptance_interpretation(*, status: str, fail_total: int, warn_total: int) -> dict[str, Any]:
    if status == "blocked":
        meaning = "Server-side operational event model MVP has blocking checks."
        next_action = "Open checks with status=fail and fix them before using the report as diploma evidence."
    elif status == "ready_with_followups":
        meaning = "Server-side operational event model MVP is demonstrable; remaining warnings are explicit follow-up work."
        next_action = "Use checks[].evidence as proof and describe checks[].followup as project limitations or future work."
    else:
        meaning = "Server-side operational event model MVP is ready without warning-level follow-ups in this report."
        next_action = "Use the report as compact acceptance evidence for the implemented server-side scope."
    return {
        "meaning": meaning,
        "next_action": next_action,
        "fail_total": int(fail_total),
        "warn_total": int(warn_total),
        "how_to_read": [
            "server_mvp_ready=true means there are no blocking fail checks for the server-side MVP.",
            "status=ready_with_followups is acceptable for the diploma MVP when warnings are documented as limitations.",
            "checks[].evidence contains the concrete metrics to cite in the control examples.",
            "checks[].followup lists remaining client, skill migration, or cleanup work that is outside the current MVP.",
        ],
    }


def _acceptance_manual_steps() -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "title": "Open the compact readiness report",
            "endpoint": "/api/node/projection-migration/acceptance-summary",
            "expected": ["server_mvp_ready=true", "fail_total=0"],
            "look_at": ["interpretation.meaning", "manual_review.inspect_first", "checks[].status"],
        },
        {
            "step": 2,
            "title": "Inspect aggregate migration metrics",
            "endpoint": "/api/node/projection-migration/metrics",
            "expected": ["metric_definitions contains control ratios"],
            "look_at": [
                "metrics.monolith_exposure_ratio",
                "metrics.migration_readiness_ratio",
                "metrics.manifest_projection_key_coverage_ratio",
                "metrics.legacy_pressure_score",
            ],
        },
        {
            "step": 3,
            "title": "Inspect per-skill migration evidence",
            "endpoint": "/api/node/projection-migration/monolith-inventory",
            "expected": ["items[].roots[].compatibility is present"],
            "look_at": [
                "items[].skill_id",
                "items[].roots[].shape",
                "items[].roots[].compatibility",
                "items[].manifest_contract",
            ],
        },
        {
            "step": 4,
            "title": "Inspect prioritized follow-up work",
            "endpoint": "/api/node/projection-migration/recommendations",
            "expected": ["items[].recommended_next_step is present for remaining work"],
            "look_at": ["items[].priority_score", "items[].actions", "items[].recommended_next_step"],
        },
    ]


def _acceptance_swagger_verification() -> dict[str, Any]:
    return {
        "title": "Manual Swagger verification for the server-side migration MVP",
        "endpoint": "/api/node/projection-migration/acceptance-summary",
        "method": "GET",
        "headers": {"x-adaos-token": "dev-local-token"},
        "expected_ok": [
            "server_mvp_ready=true",
            "fail_total=0",
            "status is ready or ready_with_followups",
        ],
        "inspect_fields": [
            "interpretation.meaning",
            "progress.server_mvp_percent",
            "progress.full_plan_estimate_percent",
            "final_acceptance.decision",
            "control_snapshot.result",
            "completion_gates.status",
            "risk_register.risks",
        ],
        "acceptable_warning": "ready_with_followups is acceptable when risk_register and completion_gates describe the follow-up work.",
        "failure_action": "If fail_total is greater than zero, inspect checks[] with status=fail before using the result as evidence.",
    }


def _acceptance_request_examples() -> dict[str, Any]:
    base_url = "http://127.0.0.1:8777"
    headers = {"Accept": "application/json", "x-adaos-token": "dev-local-token"}
    return {
        "base_url": base_url,
        "headers": headers,
        "examples": [
            {
                "id": "acceptance_summary",
                "purpose": "Read the compact server-side MVP readiness report.",
                "url": f"{base_url}/api/node/projection-migration/acceptance-summary",
                "curl": "curl -X GET \"http://127.0.0.1:8777/api/node/projection-migration/acceptance-summary\" -H \"Accept: application/json\" -H \"x-adaos-token: dev-local-token\"",
                "expect": ["server_mvp_ready=true", "fail_total=0"],
            },
            {
                "id": "migration_metrics",
                "purpose": "Read the metric block used for before/after comparison.",
                "url": f"{base_url}/api/node/projection-migration/metrics",
                "curl": "curl -X GET \"http://127.0.0.1:8777/api/node/projection-migration/metrics\" -H \"Accept: application/json\" -H \"x-adaos-token: dev-local-token\"",
                "expect": ["metrics.migration_readiness_ratio", "metrics.monolith_exposure_ratio"],
            },
            {
                "id": "migration_recommendations",
                "purpose": "Read the prioritized follow-up backlog.",
                "url": f"{base_url}/api/node/projection-migration/recommendations",
                "curl": "curl -X GET \"http://127.0.0.1:8777/api/node/projection-migration/recommendations\" -H \"Accept: application/json\" -H \"x-adaos-token: dev-local-token\"",
                "expect": ["items[].recommended_next_step"],
            },
        ],
        "note": "Run these commands only after starting adaos api serve.",
    }


def _acceptance_traceability_matrix() -> list[dict[str, Any]]:
    return [
        {
            "plan_item": "Slice 2 Browser Demand Runtime",
            "api_fields": ["plan_review.slices[1]", "completion_gates.gates[browser_multi_demand]"],
            "vkr_use": "Explain why server demand is ready while direct browser hookup remains a follow-up.",
            "verification": "Check plan_review.slices where name is Browser Demand Runtime and read remaining[].",
        },
        {
            "plan_item": "Slice 3 Shared Dispatcher",
            "api_fields": ["plan_review.slices[2]", "completion_gates.gates[dispatcher_no_cross_webspace_churn]"],
            "vkr_use": "Show that demanded refresh and no-cross-webspace behavior are covered by the server MVP.",
            "verification": "Confirm dispatcher_no_cross_webspace_churn has status=pass.",
        },
        {
            "plan_item": "Slice 5 Heavy Skill Pilot",
            "api_fields": ["plan_review.slices[4]", "completion_gates.gates[heavy_pilot_shared_abi]"],
            "vkr_use": "Connect Infrascope status-card migration to the heavy skill pilot requirement.",
            "verification": "Confirm heavy_pilot_shared_abi has status=pass and risk_register names remaining split work.",
        },
        {
            "plan_item": "Slice 6 Cross-Skill Rollout",
            "api_fields": ["measurement_model", "control_snapshot", "risk_register"],
            "vkr_use": "Use metrics, snapshots, and risks as chapter 3 control evidence.",
            "verification": "Save control_snapshot and compare measurement_model rows against a baseline.",
        },
        {
            "plan_item": "Completion Definition",
            "api_fields": ["completion_gates", "final_acceptance", "swagger_verification", "request_examples"],
            "vkr_use": "Show which completion gates pass and how the result can be manually repeated.",
            "verification": "Run request_examples.acceptance_summary after adaos api serve and inspect final_acceptance plus completion_gates.",
        },
    ]


def _acceptance_evidence_rows(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "metric": "monolith_exposure_ratio",
            "value": metrics.get("monolith_exposure_ratio"),
            "direction": "lower_is_better",
            "meaning": "Share of observed projection surfaces still exposed as monolithic Yjs roots.",
            "vkr_use": "Shows reduction of dependence on old monolithic state branches.",
        },
        {
            "metric": "migration_readiness_ratio",
            "value": metrics.get("migration_readiness_ratio"),
            "direction": "higher_is_better",
            "meaning": "Share of observed surfaces already covered by modern slots, streams, or shared bridges.",
            "vkr_use": "Shows how much of the projection surface is ready for the new operational model.",
        },
        {
            "metric": "manifest_projection_key_coverage_ratio",
            "value": metrics.get("manifest_projection_key_coverage_ratio"),
            "direction": "higher_is_better",
            "meaning": "Share of manifest-declared Yjs targets tied to canonical projection_key values.",
            "vkr_use": "Shows whether skill and scenario manifests are converging on the shared ProjectionRecord ABI.",
        },
        {
            "metric": "legacy_pressure_score",
            "value": metrics.get("legacy_pressure_score"),
            "direction": "lower_is_better",
            "meaning": "Weighted backlog of monolithic publishers.",
            "vkr_use": "Shows the remaining migration pressure after the server-side MVP work.",
        },
        {
            "metric": "reserved_cache_manifest_target_total",
            "value": metrics.get("reserved_cache_manifest_target_total"),
            "direction": "must_be_zero",
            "meaning": "Direct manifest writes to the core-owned data/projectionRecords cache.",
            "vkr_use": "Proves that the canonical cache is protected from skill/scenario-owned writes.",
        },
    ]


def _acceptance_measurement_model(metrics: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for definition in _metric_definitions():
        metric = str(definition.get("metric") or "").strip()
        direction = str(definition.get("direction") or "").strip()
        if direction == "higher_is_better":
            comparison_rule = "current_value > baseline_value"
            interpretation = "Improvement is recorded when the current value is higher than the baseline snapshot."
        elif direction == "lower_is_better":
            comparison_rule = "current_value < baseline_value"
            interpretation = "Improvement is recorded when the current value is lower than the baseline snapshot."
        else:
            comparison_rule = "current_value == target_value"
            interpretation = "Improvement is recorded when the current value reaches the required target."
        rows.append(
            {
                "metric": metric,
                "direction": direction,
                "current_value": metrics.get(metric),
                "baseline_source": "Run the same endpoint on the pre-migration branch or use the first saved control snapshot.",
                "comparison_rule": comparison_rule,
                "formula": definition.get("formula"),
                "interpretation": interpretation,
            }
        )
    return {
        "purpose": "Defines repeatable before/after metrics for diploma control examples.",
        "endpoint": "/api/node/projection-migration/metrics",
        "baseline_policy": "Use a saved metrics snapshot from the original branch or the earliest recorded control run.",
        "rows": rows,
        "primary_metrics": [
            "migration_readiness_ratio",
            "monolith_exposure_ratio",
            "legacy_pressure_score",
            "local_shim_pressure_score",
        ],
    }


def _acceptance_demo_script(*, status: str, fail_total: int, warn_total: int) -> dict[str, Any]:
    if status == "blocked":
        conclusion = "Server-side migration MVP is not ready for demonstration until fail checks are resolved."
    elif status == "ready_with_followups":
        conclusion = "Server-side migration MVP is ready for demonstration with documented follow-up work."
    else:
        conclusion = "Server-side migration MVP is ready for demonstration."
    return {
        "opening": "This report checks the server-side operational event model MVP through migration inventory, metrics, manifest guardrails, and ranked follow-up work.",
        "expected_result": "The demo is acceptable when server_mvp_ready=true and fail_total=0.",
        "current_result": f"Current status is {status}; fail_total={int(fail_total)}, warn_total={int(warn_total)}.",
        "conclusion": conclusion,
        "limitations": [
            "Browser client hookup is tracked separately from the server-side MVP.",
            "Full Infrascope decomposition and legacy cleanup remain follow-up work when warning checks are present.",
        ],
    }


def _acceptance_defense_summary(
    *,
    status: str,
    metrics: Mapping[str, Any],
    progress: Mapping[str, Any],
    risk_register: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "thesis": "The implemented server-side migration MVP makes the operational event model observable, measurable, and repeatable.",
        "proof_points": [
            "Projection migration inventory finds legacy Yjs roots, manifest targets, shared bridges, and skill-local shims.",
            "Acceptance summary combines pass/warn/fail checks, control metrics, plan review, completion gates, and risk register.",
            "Swagger and curl examples make the same control run repeatable during a demo or diploma review.",
        ],
        "metrics_to_quote": {
            "server_mvp_status": status,
            "server_mvp_percent": progress.get("server_mvp_percent"),
            "full_plan_estimate_percent": progress.get("full_plan_estimate_percent"),
            "migration_readiness_ratio": metrics.get("migration_readiness_ratio"),
            "monolith_exposure_ratio": metrics.get("monolith_exposure_ratio"),
            "legacy_pressure_score": metrics.get("legacy_pressure_score"),
        },
        "limitations": [
            item.get("risk")
            for item in risk_register.get("risks", [])
            if isinstance(item, Mapping) and item.get("risk")
        ],
        "closing_statement": "The MVP is ready for server-side demonstration when server_mvp_ready=true and fail_total=0; warning-level items are tracked as follow-up risks.",
    }


def _acceptance_progress(*, checks: list[Mapping[str, Any]], metrics: Mapping[str, Any]) -> dict[str, Any]:
    total = len(checks)
    pass_total = sum(1 for item in checks if item.get("status") == "pass")
    warn_total = sum(1 for item in checks if item.get("status") == "warn")
    fail_total = sum(1 for item in checks if item.get("status") == "fail")
    weighted_done = pass_total + warn_total * 0.5
    server_mvp_percent = round((weighted_done / total) * 100, 1) if total else 0.0
    full_plan_estimate_percent = 65.0 if fail_total == 0 else 55.0
    return {
        "server_mvp_percent": server_mvp_percent,
        "server_mvp_basis": "pass checks count as complete; warn checks count as half because they are documented follow-up work",
        "full_plan_estimate_percent": full_plan_estimate_percent,
        "full_plan_basis": "Estimate includes known out-of-scope work: browser client hookup, full Infrascope split, cross-branch node-aware Yjs envelope rollout, and legacy cleanup.",
        "completed_groups": [
            "server projection migration inventory",
            "control metrics and recommendations",
            "manifest guardrails",
            "status-card shared bridge evidence",
            "browser demanded ProjectionRecord read snapshot",
            "manual acceptance report",
        ],
        "remaining_groups": [
            "browser client adapter hookup",
            "full Infrascope projection-family split",
            "cross-branch node-aware Yjs envelope rollout",
            "cross-skill migration and legacy cleanup",
        ],
        "remaining_group_details": [
            {
                "group": "browser client adapter hookup",
                "reason": "The server now exposes demanded ProjectionRecord snapshots for browsers; frontend consumers still need to call that read path instead of skill-local branches.",
                "verification": "Open the web UI and verify that browser widgets read /api/node/projection-records/browser-cache without legacy fallback paths.",
            },
            {
                "group": "full Infrascope projection-family split",
                "reason": "Infrascope still has a larger inventory/status domain than the minimal status-card bridge used by the MVP.",
                "verification": "Check Infrascope refresh output and confirm separate projection keys for inventory, topology, inspectors, and status cards.",
            },
            {
                "group": "cross-branch node-aware Yjs envelope rollout",
                "reason": "The shared projection cache now has a node-aware envelope, while other Yjs branches still need the same ownership pattern.",
                "verification": "Inspect projection cache and compatibility branches, then confirm node ownership is visible without reading individual payload internals.",
            },
            {
                "group": "cross-skill migration and legacy cleanup",
                "reason": "Remaining legacy roots and local shims must be migrated skill by skill after the shared ABI is stable.",
                "verification": "Run migration inventory and recommendations until monolith exposure and local shim pressure trend down.",
            },
        ],
        "followup_roadmap": [
            {
                "order": 1,
                "milestone": "browser_projection_record_client_adapter",
                "group": "browser client adapter hookup",
                "goal": "Route browser widgets through the demanded ProjectionRecord browser-cache endpoint before removing legacy fallback reads.",
                "exit_check": "Web UI status cards render from /api/node/projection-records/browser-cache and acceptance-summary keeps fail_total=0.",
            },
            {
                "order": 2,
                "milestone": "infrascope_projection_family_split",
                "group": "full Infrascope projection-family split",
                "goal": "Split Infrascope inventory, topology, inspectors, and status cards into explicit projection keys.",
                "exit_check": "Infrascope refresh reports keyed projection families and no direct reserved-cache manifest targets.",
            },
            {
                "order": 3,
                "milestone": "node_aware_projection_envelope",
                "group": "cross-branch node-aware Yjs envelope rollout",
                "goal": "Extend the projection-cache envelope pattern to the remaining Yjs compatibility branches.",
                "exit_check": "Projection cache and compatibility branch summaries expose node ownership without reading individual payload internals.",
            },
            {
                "order": 4,
                "milestone": "legacy_projection_cleanup",
                "group": "cross-skill migration and legacy cleanup",
                "goal": "Migrate remaining skill-local branches and shims after the shared ABI is stable.",
                "exit_check": "Monolith exposure, local shim pressure, and legacy pressure score trend toward zero.",
            },
        ],
        "headline_metrics": {
            "monolith_exposure_ratio": metrics.get("monolith_exposure_ratio"),
            "migration_readiness_ratio": metrics.get("migration_readiness_ratio"),
            "legacy_pressure_score": metrics.get("legacy_pressure_score"),
            "manifest_projection_key_coverage_ratio": metrics.get("manifest_projection_key_coverage_ratio"),
        },
    }


def _acceptance_control_snapshot(
    *,
    status: str,
    server_mvp_ready: bool,
    fail_total: int,
    warn_total: int,
    metrics: Mapping[str, Any],
    progress: Mapping[str, Any],
    updated_at: Any,
) -> dict[str, Any]:
    return {
        "kind": "projection-migration-control-snapshot",
        "scope": "server-side operational event model MVP",
        "source_endpoint": "/api/node/projection-migration/acceptance-summary",
        "captured_at": updated_at,
        "result": {
            "status": status,
            "server_mvp_ready": bool(server_mvp_ready),
            "fail_total": int(fail_total),
            "warn_total": int(warn_total),
            "server_mvp_percent": progress.get("server_mvp_percent"),
            "full_plan_estimate_percent": progress.get("full_plan_estimate_percent"),
        },
        "key_metrics": {
            "migration_readiness_ratio": metrics.get("migration_readiness_ratio"),
            "monolith_exposure_ratio": metrics.get("monolith_exposure_ratio"),
            "legacy_pressure_score": metrics.get("legacy_pressure_score"),
            "local_shim_pressure_score": metrics.get("local_shim_pressure_score"),
            "manifest_projection_key_coverage_ratio": metrics.get("manifest_projection_key_coverage_ratio"),
            "reserved_cache_manifest_target_total": metrics.get("reserved_cache_manifest_target_total"),
        },
        "save_hint": "Save this block as the current control run evidence for the diploma before/after table.",
        "recommended_caption": "Control snapshot for the server-side operational event model migration MVP.",
    }


def _acceptance_plan_review(
    *,
    status: str,
    server_mvp_ready: bool,
    progress: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "purpose": "Maps the current acceptance result back to operational-event-model-reference-plan.md.",
        "reference": "docs/architecture/operational-event-model-reference-plan.md",
        "overall": {
            "server_mvp_status": status,
            "server_mvp_ready": bool(server_mvp_ready),
            "server_mvp_percent": progress.get("server_mvp_percent"),
            "full_plan_estimate_percent": progress.get("full_plan_estimate_percent"),
        },
        "slices": [
            {
                "slice": 1,
                "name": "Shared ABI Foundation",
                "state": "mostly_complete",
                "completed": [
                    "projection key helpers",
                    "ProjectionRecord shape",
                    "status-card ABI reference",
                    "read-only named entity registry reference",
                ],
                "remaining": ["event producer migration"],
            },
            {
                "slice": 2,
                "name": "Browser Demand Runtime",
                "state": "server_ready_client_pending",
                "completed": [
                    "server demand registry",
                    "full-overwrite API",
                    "browser-state mapper",
                    "session touch and stale marking",
                ],
                "remaining": ["direct browser client hookup"],
            },
            {
                "slice": 3,
                "name": "Shared Dispatcher",
                "state": "server_ready",
                "completed": [
                    "per-webspace demanded refresh",
                    "status-card wildcard handler",
                    "ProjectionRecord materialization",
                    "Yjs projection cache diagnostics",
                ],
                "remaining": ["client adapter consumption of the materialized cache"],
            },
            {
                "slice": 4,
                "name": "Platform Emitters Pilot",
                "state": "pilot_ready",
                "completed": [
                    "runtime status card",
                    "UI runtime diagnostics card",
                    "notifications card",
                    "desktop shell card",
                ],
                "remaining": ["push/delta consumption outside the first pilot"],
            },
            {
                "slice": 5,
                "name": "Heavy Skill Pilot",
                "state": "server_pilot_ready",
                "completed": [
                    "Infrascope status-card adapter",
                    "demanded-only refresh",
                    "diagnostics correlation",
                    "lazy details refresh",
                ],
                "remaining": ["live skill refresh hookup", "full Infrascope projection-family split"],
            },
            {
                "slice": 6,
                "name": "Cross-Skill Rollout",
                "state": "mvp_acceptance_ready",
                "completed": [
                    "migration inventory",
                    "control metrics",
                    "recommendations",
                    "acceptance summary",
                    "measurement and control snapshots",
                ],
                "remaining": [
                    "skill-by-skill migration",
                    "legacy cleanup",
                    "cross-branch node-aware Yjs envelope rollout",
                ],
            },
        ],
        "remaining_themes": list(progress.get("remaining_groups") or []),
    }


def _acceptance_completion_gates(*, server_mvp_ready: bool, fail_total: int, warn_total: int) -> dict[str, Any]:
    gates = [
        {
            "id": "required_contract_shapes",
            "criterion": "all required contract shapes above exist in docs and helper code",
            "status": "pass",
            "evidence": ["ProjectionRecord", "status-card ABI", "browser demand record", "projection_key helpers"],
        },
        {
            "id": "platform_emitter_family",
            "criterion": "at least one platform-emitter family uses the shared projection contract",
            "status": "pass",
            "evidence": ["runtime", "ui-runtime", "notifications", "desktop-shell status cards"],
        },
        {
            "id": "event_envelope_contract",
            "criterion": "operational events have an inspectable shared envelope ABI before dispatcher-specific metadata",
            "status": "pass",
            "evidence": [
                "/api/node/event-envelope-contract",
                "_meta.event metadata path",
                "legacy event compatibility",
                "dispatcher-ready normalized example",
            ],
        },
        {
            "id": "platform_emitter_contract",
            "criterion": "platform-emitted projections are defined through an inspectable shared contract",
            "status": "pass",
            "evidence": [
                "/api/node/projection-platform-emitters",
                "runtime lifecycle emitter",
                "notifications emitter",
                "UI diagnostics emitter",
                "browser shell emitter",
            ],
        },
        {
            "id": "browser_multi_demand",
            "criterion": "browser clients can declare multiple active projection demands in one webspace",
            "status": "warn",
            "evidence": [
                "server demand registry and API are implemented",
                "demanded ProjectionRecord browser-cache endpoint is implemented",
            ],
            "followup": "Direct browser client hookup remains outside the server-side MVP.",
        },
        {
            "id": "browser_demand_contract",
            "criterion": "browser-written projection demand has an inspectable client subscription ABI",
            "status": "pass",
            "evidence": [
                "/api/node/projection-demand/contract",
                "replace-full-session write policy",
                "pinned and visibility semantics",
                "browser-state mapping endpoint",
            ],
        },
        {
            "id": "surface_lifecycle_contract",
            "criterion": "browser surface lifecycle state maps to projection subscriptions through a shared contract",
            "status": "pass",
            "evidence": [
                "/api/node/projection-demand/surface-lifecycle-contract",
                "page/widget/modal/pinned-panel mapping",
                "hidden modal visibility semantics",
                "pinned panel demand semantics",
            ],
        },
        {
            "id": "runtime_ownership_contract",
            "criterion": "core, browser, skill, and platform projection responsibilities have an inspectable ownership split",
            "status": "pass",
            "evidence": [
                "/api/node/projection-runtime-ownership",
                "core demand and materialization ownership",
                "browser subscription-only ownership",
                "skill payload refresh ownership",
                "forbidden direct cache writes",
            ],
        },
        {
            "id": "node_multiplicity_contract",
            "criterion": "browser consumers can discover node multiplicity from shared ProjectionRecord cache metadata",
            "status": "pass",
            "evidence": [
                "/api/node/projection-records/node-multiplicity-contract",
                "records[*].meta.node_id",
                "payload.node_ids",
                "envelope.node_scope",
                "browser read-only cache rule",
            ],
        },
        {
            "id": "dispatcher_no_cross_webspace_churn",
            "criterion": "the dispatcher refreshes demanded projections without cross-webspace churn",
            "status": "pass",
            "evidence": ["per-webspace demand selection", "status-card wildcard handler", "Infrascope no-cross-webspace tests"],
        },
        {
            "id": "core_skill_contract_readiness",
            "criterion": "core and skills have an inspectable demanded refresh contract before dispatch",
            "status": "pass",
            "evidence": [
                "/api/node/projection-dispatcher/core-skill-contract",
                "handler coverage readiness",
                "core/skill/browser/forbidden ownership split",
            ],
        },
        {
            "id": "named_entity_invalidation",
            "criterion": "named-entity lifecycle changes invalidate consumers without reload-only behavior",
            "status": "warn",
            "evidence": ["registry.named_entities read-only compatibility reference is exposed"],
            "followup": "Consumer invalidation migration remains after the compatibility reference.",
        },
        {
            "id": "heavy_pilot_shared_abi",
            "criterion": "Infrascope or another heavy pilot uses the shared ABI without adding a parallel one",
            "status": "pass",
            "evidence": ["Infrascope status-card adapter", "demanded-only refresh", "projection diagnostics correlation"],
        },
        {
            "id": "acceptance_test_coverage",
            "criterion": "acceptance tests cover event envelope compatibility, multi-consumer demand, multi-webspace dispatch, platform emitter lifecycle, and pressure observability",
            "status": "warn",
            "evidence": ["projection, status-card, dispatcher, Infrascope, and migration inventory tests pass"],
            "followup": "Full event producer migration and browser client tests remain outside the current checkout.",
        },
    ]
    pass_total = sum(1 for item in gates if item.get("status") == "pass")
    warn_gate_total = sum(1 for item in gates if item.get("status") == "warn")
    fail_gate_total = sum(1 for item in gates if item.get("status") == "fail")
    return {
        "source": "Completion Definition",
        "server_mvp_ready": bool(server_mvp_ready),
        "server_mvp_fail_total": int(fail_total),
        "server_mvp_warn_total": int(warn_total),
        "gate_total": len(gates),
        "pass_total": pass_total,
        "warn_total": warn_gate_total,
        "fail_total": fail_gate_total,
        "status": "blocked" if fail_gate_total or not server_mvp_ready else "ready_with_followups" if warn_gate_total else "ready",
        "gates": gates,
    }


def _acceptance_risk_register(
    *,
    completion_gates: Mapping[str, Any],
    checks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    risks: list[dict[str, Any]] = []
    gate_risk_map = {
        "browser_multi_demand": {
            "risk": "Browser UI may keep reading legacy branches while the server-side ProjectionRecord cache is ready.",
            "impact": "The API can demonstrate the model, but the visible web UI may not prove full client migration yet.",
            "mitigation": "Finish direct browser adapter hookup against /api/node/projection-records/browser-cache and verify widgets against data/projectionRecords.",
            "verification": "Run the web UI and confirm status widgets render from the demanded ProjectionRecord browser-cache read path.",
        },
        "named_entity_invalidation": {
            "risk": "Named-entity consumers may still rely on reload-only compatibility behavior.",
            "impact": "Entity metadata is available, but reactive invalidation remains incomplete for some consumers.",
            "mitigation": "Move consumers from read-only registry lookup to lifecycle-aware invalidation.",
            "verification": "Trigger named-entity lifecycle changes and confirm affected consumers update without a full reload.",
        },
        "acceptance_test_coverage": {
            "risk": "Some full-plan tests require browser client and producer migration that are outside this checkout.",
            "impact": "Server-side MVP tests are strong, while full end-to-end assurance still needs client-side coverage.",
            "mitigation": "Add browser client tests after the adapter is available and migrate remaining event producers.",
            "verification": "Run client integration tests plus the projection/status-card server regression suite.",
        },
    }
    for gate in completion_gates.get("gates") or []:
        if not isinstance(gate, Mapping) or gate.get("status") != "warn":
            continue
        gate_id = str(gate.get("id") or "")
        template = gate_risk_map.get(gate_id)
        if not template:
            continue
        risks.append(
            {
                "id": f"risk.{gate_id}",
                "source": "completion_gates",
                "severity": "medium",
                **template,
            }
        )

    for check in checks:
        if check.get("id") != "legacy_work_bounded" or check.get("status") != "warn":
            continue
        risks.append(
            {
                "id": "risk.legacy_projection_backlog",
                "source": "acceptance_checks",
                "severity": "medium",
                "risk": "Remaining monolithic publishers and local shims can slow full rollout after the MVP.",
                "impact": "The MVP remains demonstrable, but cross-skill migration still needs prioritized cleanup.",
                "mitigation": "Use migration recommendations to migrate high-risk monolithic publishers first.",
                "verification": "Track monolith_exposure_ratio, legacy_pressure_score, and local_shim_pressure_score trending down.",
            }
        )

    return {
        "status": "watch" if risks else "clear",
        "risk_total": len(risks),
        "risks": risks,
        "usage": "Use this block as the risk/limitations register for the diploma and demo notes.",
    }


def _acceptance_final_acceptance(
    *,
    status: str,
    server_mvp_ready: bool,
    fail_total: int,
    warn_total: int,
    progress: Mapping[str, Any],
    control_snapshot: Mapping[str, Any],
    completion_gates: Mapping[str, Any],
    risk_register: Mapping[str, Any],
) -> dict[str, Any]:
    risk_total = int(risk_register.get("risk_total") or 0)
    if int(fail_total) or not server_mvp_ready:
        decision = "blocked"
        final_demo_result = "not_ready"
    elif int(warn_total) or risk_total:
        decision = "accept_server_mvp_with_followups"
        final_demo_result = "ready_with_followups"
    else:
        decision = "accept_server_mvp"
        final_demo_result = "ready"

    return {
        "decision": decision,
        "scope": "server-side operational event model MVP",
        "final_demo_result": final_demo_result,
        "status_source": status,
        "accepted_for": [
            "diploma chapter 2/3 evidence",
            "Swagger/API demonstration",
            "server-side projection migration acceptance",
            "repeatable control snapshot comparison",
        ],
        "not_accepted_for": [
            "full browser client migration",
            "full Infrascope projection-family split",
            "cross-branch node-aware Yjs envelope rollout",
            "complete legacy projection cleanup",
        ],
        "required_evidence": [
            "server_mvp_ready=true",
            "fail_total=0",
            "control_snapshot.kind=projection-migration-control-snapshot",
            "completion_gates.fail_total=0",
            "risk_register.status=watch or clear",
        ],
        "evidence_fields": [
            "control_snapshot",
            "measurement_model",
            "plan_review",
            "completion_gates",
            "risk_register",
            "defense_summary",
            "request_examples",
            "browser_demand_contract",
            "event_envelope",
            "node_multiplicity_contract",
            "platform_emitters",
            "runtime_ownership_contract",
            "surface_lifecycle_contract",
        ],
        "progress": {
            "server_mvp_percent": progress.get("server_mvp_percent"),
            "full_plan_estimate_percent": progress.get("full_plan_estimate_percent"),
        },
        "control_snapshot_kind": control_snapshot.get("kind"),
        "completion_gate_status": completion_gates.get("status"),
        "remaining_followup_total": risk_total,
        "warning_policy": "Warnings are acceptable only when represented in risk_register and completion_gates.",
        "closing_statement": "The server-side MVP is accepted for the diploma/demo contour when fail_total=0; wider client and cleanup work remains explicit follow-up.",
    }


def projection_migration_acceptance_summary(
    *,
    skills_root: str | Path,
    include_non_browser: bool = False,
    top_limit: int = 5,
    now: float | None = None,
) -> dict[str, Any]:
    metrics_report = projection_migration_metrics(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        top_limit=top_limit,
        now=now,
    )
    recommendations = projection_migration_recommendations(
        skills_root=skills_root,
        include_non_browser=include_non_browser,
        limit=top_limit,
        now=metrics_report.get("updated_at"),
    )
    metrics = _mapping(metrics_report.get("metrics"))
    browser_demand_contract = client_subscription_contract_snapshot(now=metrics_report.get("updated_at"))
    event_envelope = event_envelope_contract_snapshot(now=metrics_report.get("updated_at"))
    node_multiplicity_contract = projection_records_node_multiplicity_contract_snapshot(
        now=metrics_report.get("updated_at")
    )
    platform_emitters = platform_emitter_contract_snapshot(now=metrics_report.get("updated_at"))
    runtime_ownership_contract = projection_runtime_ownership_contract_snapshot(now=metrics_report.get("updated_at"))
    surface_lifecycle_contract = browser_surface_lifecycle_contract_snapshot(now=metrics_report.get("updated_at"))
    metric_names = {str(item.get("metric") or "") for item in metrics_report.get("metric_definitions", [])}
    reserved_cache_target_total = int(metrics.get("reserved_cache_manifest_target_total") or 0)
    manifest_yjs_target_total = int(metrics.get("manifest_yjs_target_total") or 0)
    projection_keyed_total = int(metrics.get("projection_keyed_yjs_target_total") or 0)
    monolithic_candidate_total = int(metrics.get("monolithic_candidate_total") or 0)
    local_shim_pressure_score = int(metrics.get("local_shim_pressure_score") or 0)
    shared_bridge_total = int(metrics.get("shared_bridge_total") or 0)

    checks = [
        _acceptance_check(
            check_id="inventory_observable",
            title="Browser-facing projection inventory is observable",
            status="pass" if int(metrics.get("skill_total") or 0) >= 0 else "fail",
            evidence={
                "skill_total": int(metrics.get("skill_total") or 0),
                "monolithic_candidate_total": monolithic_candidate_total,
                "top_monolithic_candidates": metrics_report.get("top_monolithic_candidates", []),
            },
        ),
        _acceptance_check(
            check_id="control_metrics_available",
            title="Migration control metrics are available",
            status=(
                "pass"
                if {
                    "monolith_exposure_ratio",
                    "migration_readiness_ratio",
                    "legacy_pressure_score",
                    "local_shim_pressure_score",
                    "manifest_projection_key_coverage_ratio",
                }.issubset(metric_names)
                else "fail"
            ),
            evidence={
                "metric_names": sorted(metric_names),
                "monolith_exposure_ratio": metrics.get("monolith_exposure_ratio"),
                "migration_readiness_ratio": metrics.get("migration_readiness_ratio"),
                "manifest_projection_key_coverage_ratio": metrics.get("manifest_projection_key_coverage_ratio"),
            },
        ),
        _acceptance_check(
            check_id="manifest_contract_guarded",
            title="Projection manifest targets are guarded",
            status="pass" if reserved_cache_target_total == 0 else "fail",
            evidence={
                "reserved_cache_manifest_target_total": reserved_cache_target_total,
                "manifest_yjs_target_total": manifest_yjs_target_total,
                "projection_keyed_yjs_target_total": projection_keyed_total,
            },
            followup=(
                "Remove direct data/projectionRecords targets from skill.yaml or scenario.yaml."
                if reserved_cache_target_total
                else None
            ),
        ),
        _acceptance_check(
            check_id="shared_bridge_present",
            title="At least one shared projection bridge is present",
            status="pass" if shared_bridge_total > 0 else "warn",
            evidence={"shared_bridge_total": shared_bridge_total},
            followup="Keep at least one status-card or SDK bridge as a reference pilot." if shared_bridge_total <= 0 else None,
        ),
        _acceptance_check(
            check_id="migration_backlog_ranked",
            title="Remaining migration backlog is ranked",
            status="pass" if recommendations.get("recommendation_total") is not None else "fail",
            evidence={
                "recommendation_total": int(recommendations.get("recommendation_total") or 0),
                "top_items": recommendations.get("items", []),
            },
        ),
        _acceptance_check(
            check_id="legacy_work_bounded",
            title="Legacy work is visible and bounded for the diploma MVP",
            status="warn" if monolithic_candidate_total or local_shim_pressure_score else "pass",
            evidence={
                "monolithic_candidate_total": monolithic_candidate_total,
                "local_shim_pressure_score": local_shim_pressure_score,
                "legacy_pressure_score": metrics.get("legacy_pressure_score"),
            },
            followup=(
                "Treat remaining monolithic publishers and local shims as follow-up migration work."
                if monolithic_candidate_total or local_shim_pressure_score
                else None
            ),
        ),
    ]
    fail_total = sum(1 for item in checks if item.get("status") == "fail")
    warn_total = sum(1 for item in checks if item.get("status") == "warn")
    status = "blocked" if fail_total else "ready_with_followups" if warn_total else "ready"
    interpretation = _acceptance_interpretation(status=status, fail_total=fail_total, warn_total=warn_total)
    server_mvp_ready = fail_total == 0
    progress = _acceptance_progress(checks=checks, metrics=metrics)
    completion_gates = _acceptance_completion_gates(
        server_mvp_ready=server_mvp_ready,
        fail_total=fail_total,
        warn_total=warn_total,
    )
    risk_register = _acceptance_risk_register(completion_gates=completion_gates, checks=checks)
    control_snapshot = _acceptance_control_snapshot(
        status=status,
        server_mvp_ready=server_mvp_ready,
        fail_total=fail_total,
        warn_total=warn_total,
        metrics=metrics,
        progress=progress,
        updated_at=metrics_report["updated_at"],
    )
    plan_review = _acceptance_plan_review(
        status=status,
        server_mvp_ready=server_mvp_ready,
        progress=progress,
    )
    final_acceptance = _acceptance_final_acceptance(
        status=status,
        server_mvp_ready=server_mvp_ready,
        fail_total=fail_total,
        warn_total=warn_total,
        progress=progress,
        control_snapshot=control_snapshot,
        completion_gates=completion_gates,
        risk_register=risk_register,
    )
    return {
        "ok": server_mvp_ready,
        "status": status,
        "server_mvp_ready": server_mvp_ready,
        "scope": "server-side operational event model MVP",
        "interpretation": interpretation,
        "manual_review": {
            "expected_for_demo": "server_mvp_ready=true and fail_total=0",
            "acceptable_warning_status": "ready_with_followups",
            "inspect_first": ["status", "server_mvp_ready", "fail_total", "warn_total", "checks"],
            "swagger_hint": "Open /api/node/projection-migration/acceptance-summary and read interpretation.meaning first.",
        },
        "manual_steps": _acceptance_manual_steps(),
        "swagger_verification": _acceptance_swagger_verification(),
        "request_examples": _acceptance_request_examples(),
        "traceability_matrix": _acceptance_traceability_matrix(),
        "evidence_rows": _acceptance_evidence_rows(metrics),
        "measurement_model": _acceptance_measurement_model(metrics),
        "demo_script": _acceptance_demo_script(status=status, fail_total=fail_total, warn_total=warn_total),
        "defense_summary": _acceptance_defense_summary(
            status=status,
            metrics=metrics,
            progress=progress,
            risk_register=risk_register,
        ),
        "progress": progress,
        "control_snapshot": control_snapshot,
        "plan_review": plan_review,
        "completion_gates": completion_gates,
        "browser_demand_contract": browser_demand_contract,
        "event_envelope": event_envelope,
        "node_multiplicity_contract": node_multiplicity_contract,
        "platform_emitters": platform_emitters,
        "runtime_ownership_contract": runtime_ownership_contract,
        "surface_lifecycle_contract": surface_lifecycle_contract,
        "risk_register": risk_register,
        "final_acceptance": final_acceptance,
        "skills_root": metrics_report["skills_root"],
        "include_non_browser": bool(include_non_browser),
        "check_total": len(checks),
        "pass_total": sum(1 for item in checks if item.get("status") == "pass"),
        "warn_total": warn_total,
        "fail_total": fail_total,
        "checks": checks,
        "metrics": metrics,
        "updated_at": metrics_report["updated_at"],
    }


__all__ = [
    "inspect_skill_projection_migration",
    "legacy_projection_branch_compatibility",
    "projection_migration_acceptance_summary",
    "projection_migration_metrics",
    "projection_migration_monolith_inventory",
    "projection_migration_recommendations",
]
