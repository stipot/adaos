# \src\adaos\services\scenario\projection_registry.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional
from adaos.services.scenarios.loader import read_manifest


ProjectionBackend = Literal["yjs", "kv", "sql"]
PROJECTION_MANIFEST_SCHEMA = "adaos.data-projections.v1"
PROJECTION_RECORDS_MANIFEST_PATH = "data/projectionRecords"


@dataclass(slots=True)
class ProjectionTarget:
    """
    Single physical projection target for a (scope, slot) pair.

    backend:
      - "yjs"  — project into a YDoc path,
      - "kv"   — project into a KV key,
      - "sql"  — project into a SQL table/column (reserved for future use).
    """

    backend: ProjectionBackend
    webspace_id: Optional[str] = None
    path: Optional[str] = None
    projection_key: Optional[str] = None
    table: Optional[str] = None
    column: Optional[str] = None


@dataclass(slots=True)
class ProjectionRule:
    scope: str
    slot: str
    targets: List[ProjectionTarget]


class ProjectionRegistry:
    """
    Registry that maps (scope, slot) pairs used by ctx.*.set/get to
    concrete storage targets (Yjs paths, KV keys, SQL rows, ...).

    For the MVP this is a lightweight, read-only facade over scenario
    manifests: if a scenario.yaml defines a `data_projections` section,
    entries from there are loaded into this registry.
    """

    def __init__(self) -> None:
        self._rules: Dict[tuple[str, str], ProjectionRule] = {}
        self._scenario_rules: Dict[tuple[str, str], ProjectionRule] = {}
        self._active_scenario_id: Optional[str] = None
        self._active_space: str = "workspace"

    def load_entries(self, entries: list[dict]) -> int:
        """
        Load projection rules from a generic ``data_projections``-like list.

        This helper is shared between scenario manifests and skill manifests
        so that skills can define default projections and scenarios can
        override them by calling :meth:`load_from_scenario` later.
        """
        raw = entries or []
        if not isinstance(raw, list):
            return 0

        loaded = 0
        for item in raw:
            if not isinstance(item, dict):
                continue

            scope = str(item.get("scope") or "").strip()
            slot = str(item.get("slot") or "").strip()
            if not scope or not slot:
                continue

            targets_raw = item.get("targets") or []
            if not isinstance(targets_raw, list):
                continue

            targets: List[ProjectionTarget] = []
            for t in targets_raw:
                if not isinstance(t, dict):
                    continue
                backend = str(t.get("backend") or "").strip().lower()
                if backend not in ("yjs", "kv", "sql"):
                    continue
                targets.append(
                    ProjectionTarget(
                        backend=backend,  # type: ignore[arg-type]
                        webspace_id=str(t.get("webspace_id") or "") or None,
                        path=str(t.get("path") or "") or None,
                        projection_key=str(t.get("projection_key") or "") or None,
                        table=str(t.get("table") or "") or None,
                        column=str(t.get("column") or "") or None,
                    )
                )
            key = (scope, slot)
            if targets:
                self._rules[key] = ProjectionRule(scope=scope, slot=slot, targets=targets)
                loaded += 1
        return loaded

    def replace_scenario_entries(
        self,
        entries: list[dict],
        *,
        scenario_id: Optional[str] = None,
        space: str = "workspace",
    ) -> int:
        """
        Replace the active scenario override layer.

        Skill-level defaults are stored in ``_rules`` and remain intact.
        Scenario manifests act as a single active override layer so switching
        to a scenario without ``data_projections`` correctly clears stale rules
        from the previous scenario.
        """
        self._scenario_rules = {}
        self._active_scenario_id = str(scenario_id or "").strip() or None
        self._active_space = "dev" if str(space or "").strip().lower() == "dev" else "workspace"

        raw = entries or []
        if not isinstance(raw, list):
            return 0

        loaded = 0
        for item in raw:
            if not isinstance(item, dict):
                continue

            scope = str(item.get("scope") or "").strip()
            slot = str(item.get("slot") or "").strip()
            if not scope or not slot:
                continue

            targets_raw = item.get("targets") or []
            if not isinstance(targets_raw, list):
                continue

            targets: List[ProjectionTarget] = []
            for t in targets_raw:
                if not isinstance(t, dict):
                    continue
                backend = str(t.get("backend") or "").strip().lower()
                if backend not in ("yjs", "kv", "sql"):
                    continue
                targets.append(
                    ProjectionTarget(
                        backend=backend,  # type: ignore[arg-type]
                        webspace_id=str(t.get("webspace_id") or "") or None,
                        path=str(t.get("path") or "") or None,
                        projection_key=str(t.get("projection_key") or "") or None,
                        table=str(t.get("table") or "") or None,
                        column=str(t.get("column") or "") or None,
                    )
                )

            key = (scope, slot)
            if targets:
                self._scenario_rules[key] = ProjectionRule(scope=scope, slot=slot, targets=targets)
                loaded += 1
        return loaded

    def load_from_scenario(self, scenario_id: str, *, space: str = "workspace") -> int:
        """
        Load projection rules from scenario.yaml for the given scenario id.

        Expected shape (optional) inside scenario.yaml:

        data_projections:
          - scope: subnet
            slot: weather.snapshot
            targets:
              - backend: yjs
                webspace_id: desktop
                path: data/skills/weather/global/snapshot
        """
        manifest = read_manifest(scenario_id, space=space)
        entries = manifest.get("data_projections") or []
        return self.replace_scenario_entries(entries, scenario_id=scenario_id, space=space)

    def resolve(self, scope: str, slot: str) -> List[ProjectionTarget]:
        """
        Resolve a (scope, slot) pair to a list of projection targets.

        If no rule is present, returns an empty list; callers should treat
        this as "no projections configured".
        """
        key = (str(scope).strip(), str(slot).strip())
        rule = self._scenario_rules.get(key) or self._rules.get(key)
        return list(rule.targets) if rule else []

    def active_scenario_id(self) -> Optional[str]:
        return self._active_scenario_id

    def active_space(self) -> str:
        return self._active_space

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": PROJECTION_MANIFEST_SCHEMA,
            "active_scenario_id": self._active_scenario_id,
            "active_space": self._active_space,
            "base_rule_count": len(self._rules),
            "scenario_rule_count": len(self._scenario_rules),
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_yjs_path(value: Any) -> str | None:
    token = _text(value).replace("\\", "/")
    if token.startswith("y:"):
        token = token[2:].strip()
    token = token.strip("/")
    if not token.startswith("data/"):
        return None
    return token


def _yjs_target_classification(path: str, *, projection_key: str | None = None) -> dict[str, Any]:
    if path == PROJECTION_RECORDS_MANIFEST_PATH:
        return {
            "classification": "reserved-projection-record-cache",
            "accepted": False,
            "severity": "error",
            "finding": "reserved_projection_record_cache_target",
            "message": "data/projectionRecords is core-owned and must not be targeted by skill or scenario manifests.",
        }
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2:
        classification = "legacy-monolithic-yjs-root"
        severity = "warning"
        finding = "legacy_monolithic_yjs_root"
        message = "Use a narrower Yjs path and stable projection_key before removing legacy read compatibility."
    elif projection_key:
        classification = "projection-keyed-yjs-target"
        severity = "info"
        finding = None
        message = "Yjs target is tied to a canonical projection_key."
    else:
        classification = "sectioned-yjs-target"
        severity = "info"
        finding = None
        message = "Yjs target is sectioned; add projection_key when it becomes part of the shared projection contract."
    return {
        "classification": classification,
        "accepted": True,
        "severity": severity,
        "finding": finding,
        "message": message,
    }


def projection_manifest_contract() -> dict[str, Any]:
    return {
        "schema": PROJECTION_MANIFEST_SCHEMA,
        "logical_identity": ["scope", "slot"],
        "target_backends": ["yjs", "kv", "sql"],
        "yjs_rules": {
            "allowed_root": "data/<owner-or-family>",
            "preferred_shape": "data/<owner-or-family>/<section>",
            "canonical_projection_key": "target.projection_key",
            "reserved_paths": [PROJECTION_RECORDS_MANIFEST_PATH],
            "reserved_path_policy": "core-owned-cache-only",
        },
        "override_order": ["skill.yaml defaults", "scenario.yaml active override"],
    }


def inspect_projection_manifest_entries(entries: Any) -> dict[str, Any]:
    raw = entries if isinstance(entries, list) else []
    rules: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    yjs_target_total = 0
    yjs_target_with_projection_key_total = 0
    reserved_cache_target_total = 0
    legacy_monolithic_target_total = 0
    accepted_target_total = 0

    for index, item in enumerate(raw):
        data = item if isinstance(item, Mapping) else {}
        scope = _text(data.get("scope"))
        slot = _text(data.get("slot"))
        rule = {
            "index": index,
            "scope": scope,
            "slot": slot,
            "accepted": bool(scope and slot),
            "targets": [],
        }
        if not rule["accepted"]:
            findings.append(
                {
                    "severity": "error",
                    "finding": "missing_scope_or_slot",
                    "index": index,
                    "message": "Projection manifest entries must declare both scope and slot.",
                }
            )
        targets = data.get("targets") if isinstance(data.get("targets"), list) else []
        for target_index, target in enumerate(targets):
            target_data = target if isinstance(target, Mapping) else {}
            backend = _text(target_data.get("backend")).lower()
            projection_key = _text(target_data.get("projection_key")) or None
            target_report: dict[str, Any] = {
                "index": target_index,
                "backend": backend,
                "projection_key": projection_key,
                "accepted": backend in ("yjs", "kv", "sql"),
            }
            if backend not in ("yjs", "kv", "sql"):
                target_report.update(
                    {
                        "classification": "unsupported-backend",
                        "severity": "error",
                        "finding": "unsupported_backend",
                        "message": "Projection target backend must be yjs, kv, or sql.",
                    }
                )
            elif backend == "yjs":
                yjs_target_total += 1
                if projection_key:
                    yjs_target_with_projection_key_total += 1
                path = _normalize_yjs_path(target_data.get("path"))
                target_report["path"] = path
                if not path:
                    target_report.update(
                        {
                            "accepted": False,
                            "classification": "invalid-yjs-path",
                            "severity": "error",
                            "finding": "invalid_yjs_path",
                            "message": "Yjs projection targets must use data/<root> paths.",
                        }
                    )
                else:
                    target_report.update(_yjs_target_classification(path, projection_key=projection_key))
                    if target_report.get("finding") == "reserved_projection_record_cache_target":
                        reserved_cache_target_total += 1
                    if target_report.get("finding") == "legacy_monolithic_yjs_root":
                        legacy_monolithic_target_total += 1
            if target_report.get("accepted"):
                accepted_target_total += 1
            if target_report.get("finding"):
                findings.append(
                    {
                        "severity": target_report.get("severity"),
                        "finding": target_report.get("finding"),
                        "index": index,
                        "target_index": target_index,
                        "scope": scope,
                        "slot": slot,
                        "message": target_report.get("message"),
                    }
                )
            rule["targets"].append(target_report)
        rules.append(rule)

    return {
        "schema": PROJECTION_MANIFEST_SCHEMA,
        "ok": not any(item.get("severity") == "error" for item in findings),
        "rule_total": len(rules),
        "accepted_rule_total": sum(1 for item in rules if item.get("accepted")),
        "target_total": sum(len(item.get("targets", [])) for item in rules),
        "accepted_target_total": accepted_target_total,
        "yjs_target_total": yjs_target_total,
        "yjs_target_with_projection_key_total": yjs_target_with_projection_key_total,
        "reserved_cache_target_total": reserved_cache_target_total,
        "legacy_monolithic_target_total": legacy_monolithic_target_total,
        "rules": rules,
        "findings": findings,
        "contract": projection_manifest_contract(),
    }


__all__ = [
    "PROJECTION_MANIFEST_SCHEMA",
    "PROJECTION_RECORDS_MANIFEST_PATH",
    "ProjectionBackend",
    "ProjectionTarget",
    "ProjectionRule",
    "ProjectionRegistry",
    "inspect_projection_manifest_entries",
    "projection_manifest_contract",
]
