from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping

from adaos.services import device_inventory

ENTITY_OBSERVED = "entity.observed"
ENTITY_DRAFT_NAME_SUGGESTED = "entity.draft_name.suggested"
ENTITY_DISPLAY_NAME_CHANGED = "entity.display_name.changed"
ENTITY_ALIAS_ADDED = "entity.alias.added"
ENTITY_ALIAS_REMOVED = "entity.alias.removed"
ENTITY_ALIAS_DEPRECATED = "entity.alias.deprecated"
ENTITY_ALIAS_CONFLICT_DETECTED = "entity.alias.conflict.detected"
ENTITY_REGISTRY_CHANGED = "entity.registry.changed"
ENTITY_RESOLUTION_AMBIGUOUS = "entity.resolution.ambiguous"
ENTITY_RESOLUTION_FAILED = "entity.resolution.failed"
NAMED_ENTITY_REGISTRY_SCHEMA = "adaos.named-entity-registry.v1"
NAMED_ENTITY_REGISTRY_YJS_PATH = "registry.named_entities"

ENTITY_EVENT_TOPICS: tuple[str, ...] = (
    ENTITY_OBSERVED,
    ENTITY_DRAFT_NAME_SUGGESTED,
    ENTITY_DISPLAY_NAME_CHANGED,
    ENTITY_ALIAS_ADDED,
    ENTITY_ALIAS_REMOVED,
    ENTITY_ALIAS_DEPRECATED,
    ENTITY_ALIAS_CONFLICT_DETECTED,
    ENTITY_REGISTRY_CHANGED,
    ENTITY_RESOLUTION_AMBIGUOUS,
    ENTITY_RESOLUTION_FAILED,
)

EntityStatus = Literal["draft", "confirmed", "observed", "conflicted", "deprecated"]
EntityAliasAction = Literal["alias.add", "alias.remove", "alias.deprecate"]
EntityAliasProposalStatus = Literal["proposed", "conflict", "noop", "invalid", "not_found", "stale"]
EntityAliasApplyStatus = Literal["applied", "conflict", "noop", "invalid", "not_found", "stale"]
EntityMatchType = Literal[
    "display_name",
    "registered_name",
    "observed_name",
    "draft_name",
    "alias",
    "fallback_label",
]
EntityLabelRole = Literal["display", "registered", "observed", "draft", "alias", "fallback"]


_SPACE_RE = re.compile(r"\s+")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_or_none(value: Any) -> str | None:
    token = _text(value)
    return token or None


def _tuple_of_texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable):
        items = list(value)
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        token = _text(item)
        if not token:
            continue
        folded = normalize_entity_label(token)
        if folded in seen:
            continue
        seen.add(folded)
        out.append(token)
    return tuple(out)


def _locale(value: Any) -> str:
    token = _text(value)
    return token or "und"


def _preferred_locales(
    *,
    request_locale: str | None = None,
    preferred_locales: Iterable[str] | None = None,
) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    raw_values: list[str | None] = [request_locale]
    raw_values.extend(str(item) for item in tuple(preferred_locales or ()))
    for raw in raw_values:
        token = _locale(raw)
        folded = token.casefold()
        if not token or token == "und" or folded in seen:
            continue
        seen.add(folded)
        out.append(token)
        base = token.split("-", 1)[0]
        if base and base != token and base.casefold() not in seen:
            seen.add(base.casefold())
            out.append(base)
    return tuple(out)


def _locale_match_score(label_locale: str, preferred: tuple[str, ...]) -> float:
    locale = _locale(label_locale)
    if locale == "und":
        return 1.0
    if not preferred:
        return 0.95
    folded = locale.casefold()
    preferred_folded = {item.casefold() for item in preferred}
    if folded in preferred_folded:
        return 1.0
    base = locale.split("-", 1)[0].casefold()
    if base in preferred_folded:
        return 0.98
    return 0.9


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def normalize_entity_label(value: Any) -> str:
    token = _SPACE_RE.sub(" ", _text(value))
    return token.casefold()


def canonical_device_ref(device_ref: str) -> str:
    parsed = device_inventory.parse_device_ref(device_ref)
    if parsed is None:
        token = _text(device_ref)
        return token
    kind, link_id = parsed
    return f"device:{kind}:{link_id}"


def compatibility_device_ref(canonical_ref: str) -> str:
    token = _text(canonical_ref)
    prefix = "device:"
    if not token.startswith(prefix):
        return token
    return token[len(prefix) :]


def _title_token(value: Any) -> str:
    token = _text(value)
    if not token:
        return ""
    known = {
        "android": "Android",
        "chrome": "Chrome",
        "edge": "Edge",
        "firefox": "Firefox",
        "ios": "iOS",
        "iphone": "iPhone",
        "ipad": "iPad",
        "linux": "Linux",
        "mac": "Mac",
        "macos": "macOS",
        "safari": "Safari",
        "tablet": "Tablet",
        "windows": "Windows",
    }
    folded = token.casefold().replace("_", " ").replace("-", " ")
    return known.get(folded, " ".join(part.capitalize() for part in folded.split()))


def _browser_draft_name(identity: Mapping[str, Any], *, fallback: str) -> str | None:
    browser = _title_token(
        identity.get("browser_family")
        or identity.get("browser_name")
        or identity.get("browser")
    )
    os_name = _title_token(identity.get("os_name") or identity.get("os") or identity.get("platform"))
    form_factor = _title_token(identity.get("form_factor") or identity.get("device_type"))
    if form_factor.casefold() in {"desktop", "computer", "pc"}:
        form_factor = ""
    if browser and os_name and form_factor:
        return f"{browser} on {os_name} {form_factor}"
    if browser and os_name:
        return f"{browser} on {os_name}"
    if browser:
        return f"{browser} browser"
    if os_name:
        return f"Browser on {os_name}"
    short_id = _text(fallback)
    if short_id:
        return f"Browser {short_id[-6:]}" if len(short_id) > 6 else f"Browser {short_id}"
    return None


@dataclass(frozen=True)
class EntityLabel:
    text: str
    locale: str = "und"
    role: EntityLabelRole = "alias"
    status: EntityStatus = "confirmed"
    source: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _text(self.text))
        object.__setattr__(self, "locale", _locale(self.locale))
        object.__setattr__(self, "role", _text(self.role) or "alias")
        object.__setattr__(self, "status", _text(self.status) or "confirmed")
        object.__setattr__(self, "source", _text_or_none(self.source))

    @property
    def match_type(self) -> EntityMatchType:
        role = self.role
        if role == "display":
            return "display_name"
        if role == "registered":
            return "registered_name"
        if role == "observed":
            return "observed_name"
        if role == "draft":
            return "draft_name"
        if role == "fallback":
            return "fallback_label"
        return "alias"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": self.text,
            "locale": self.locale,
            "role": self.role,
            "status": self.status,
        }
        if self.source:
            payload["source"] = self.source
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        return payload


def _tuple_of_labels(value: Any) -> tuple[EntityLabel, ...]:
    if value is None:
        return ()
    if isinstance(value, (EntityLabel, Mapping, str)):
        items = [value]
    elif isinstance(value, Iterable):
        items = list(value)
    else:
        items = []
    labels: list[EntityLabel] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if isinstance(item, EntityLabel):
            label = item
        elif isinstance(item, Mapping):
            label = EntityLabel(
                text=item.get("text") or item.get("label") or item.get("value"),
                locale=item.get("locale") or "und",
                role=item.get("role") or "alias",
                status=item.get("status") or "confirmed",
                source=item.get("source"),
                confidence=item.get("confidence") if isinstance(item.get("confidence"), (int, float)) else None,
            )
        else:
            label = EntityLabel(text=item, locale="und", role="alias")
        normalized = normalize_entity_label(label.text)
        if not normalized:
            continue
        key = (normalized, label.locale.casefold(), str(label.role))
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return tuple(labels)


@dataclass(frozen=True)
class NamedEntityRecord:
    canonical_ref: str
    kind: str
    display_name: str | None = None
    registered_names: tuple[str, ...] = field(default_factory=tuple)
    observed_name: str | None = None
    draft_name: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    labels: tuple[EntityLabel, ...] = field(default_factory=tuple)
    fallback_label: str | None = None
    scope: Mapping[str, Any] = field(default_factory=dict)
    source: str | None = None
    source_authority: Mapping[str, Any] = field(default_factory=dict)
    status: EntityStatus = "observed"
    updated_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_ref", _text(self.canonical_ref))
        object.__setattr__(self, "kind", _text(self.kind))
        object.__setattr__(self, "display_name", _text_or_none(self.display_name))
        object.__setattr__(self, "registered_names", _tuple_of_texts(self.registered_names))
        object.__setattr__(self, "observed_name", _text_or_none(self.observed_name))
        object.__setattr__(self, "draft_name", _text_or_none(self.draft_name))
        object.__setattr__(self, "aliases", _tuple_of_texts(self.aliases))
        object.__setattr__(self, "labels", _tuple_of_labels(self.labels))
        object.__setattr__(self, "fallback_label", _text_or_none(self.fallback_label))
        object.__setattr__(self, "scope", _mapping(self.scope))
        object.__setattr__(self, "source", _text_or_none(self.source))
        object.__setattr__(self, "source_authority", _mapping(self.source_authority))

    @property
    def display_label(self) -> str:
        return resolve_display_label(
            display_name=self.display_name,
            registered_names=self.registered_names,
            observed_name=self.observed_name,
            draft_name=self.draft_name,
            fallback_label=self.fallback_label or self.canonical_ref,
        )

    def label_records(self, *, include_fallback: bool = False) -> tuple[EntityLabel, ...]:
        candidates: list[EntityLabel] = []
        if self.display_name:
            display_status: EntityStatus = (
                self.status if self.status in {"confirmed", "conflicted", "deprecated"} else "confirmed"
            )
            candidates.append(
                EntityLabel(
                    text=self.display_name,
                    locale="und",
                    role="display",
                    status=display_status,
                    source=self.source_authority.get("display_name") or self.source,
                )
            )
        candidates.extend(
            EntityLabel(
                text=item,
                locale="und",
                role="registered",
                status="confirmed",
                source=self.source_authority.get("registered_names") or self.source,
            )
            for item in self.registered_names
        )
        if self.observed_name:
            candidates.append(
                EntityLabel(
                    text=self.observed_name,
                    locale="und",
                    role="observed",
                    status="observed",
                    source=self.source_authority.get("observed_name") or self.source,
                )
            )
        if self.draft_name:
            candidates.append(
                EntityLabel(
                    text=self.draft_name,
                    locale="und",
                    role="draft",
                    status="draft",
                    source=self.source_authority.get("draft_name") or self.source,
                )
            )
        candidates.extend(
            EntityLabel(text=item, locale="und", role="alias", status="confirmed", source=self.source)
            for item in self.aliases
        )
        candidates.extend(self.labels)
        if include_fallback and self.fallback_label:
            candidates.append(
                EntityLabel(text=self.fallback_label, locale="und", role="fallback", status="observed", source="fallback")
            )

        out: list[EntityLabel] = []
        seen: set[tuple[str, str, str]] = set()
        for label in candidates:
            normalized = normalize_entity_label(label.text)
            key = (normalized, label.locale.casefold(), label.role)
            if not normalized or key in seen:
                continue
            seen.add(key)
            out.append(label)
        return tuple(out)

    def label_candidates(self, *, include_fallback: bool = False) -> tuple[tuple[str, EntityMatchType], ...]:
        return tuple((label.text, label.match_type) for label in self.label_records(include_fallback=include_fallback))

    @property
    def fingerprint(self) -> str:
        payload = {
            "canonical_ref": self.canonical_ref,
            "kind": self.kind,
            "display_name": self.display_name,
            "registered_names": list(self.registered_names),
            "observed_name": self.observed_name,
            "draft_name": self.draft_name,
            "aliases": list(self.aliases),
            "labels": [item.to_dict() for item in self.label_records(include_fallback=False)],
            "fallback_label": self.fallback_label,
            "scope": dict(self.scope),
            "source": self.source,
            "status": self.status,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_ref": self.canonical_ref,
            "kind": self.kind,
            "display_name": self.display_name,
            "registered_names": list(self.registered_names),
            "observed_name": self.observed_name,
            "draft_name": self.draft_name,
            "aliases": list(self.aliases),
            "labels": [item.to_dict() for item in self.label_records(include_fallback=False)],
            "fallback_label": self.fallback_label,
            "display_label": self.display_label,
            "scope": dict(self.scope),
            "source": self.source,
            "source_authority": dict(self.source_authority),
            "status": self.status,
            "updated_at": self.updated_at,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class EntityResolutionMatch:
    canonical_ref: str
    kind: str
    text: str
    normalized: str
    start: int
    end: int
    confidence: float
    match_type: EntityMatchType
    display_label: str
    locale: str = "und"

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_ref": self.canonical_ref,
            "kind": self.kind,
            "text": self.text,
            "normalized": self.normalized,
            "start": self.start,
            "end": self.end,
            "confidence": self.confidence,
            "match_type": self.match_type,
            "display_label": self.display_label,
            "locale": self.locale,
        }


@dataclass(frozen=True)
class EntityResolutionAmbiguity:
    text: str
    normalized: str
    candidates: tuple[EntityResolutionMatch, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "normalized": self.normalized,
            "candidates": [item.to_dict() for item in self.candidates],
            "locales": sorted({item.locale for item in self.candidates}),
        }


@dataclass(frozen=True)
class EntityResolutionResult:
    raw_text: str
    normalized_text: str
    resolved_entities: tuple[EntityResolutionMatch, ...] = field(default_factory=tuple)
    unresolved_entity_spans: tuple[str, ...] = field(default_factory=tuple)
    ambiguities: tuple[EntityResolutionAmbiguity, ...] = field(default_factory=tuple)
    request_locale: str | None = None
    preferred_locales: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "resolved_entities": [item.to_dict() for item in self.resolved_entities],
            "unresolved_entity_spans": list(self.unresolved_entity_spans),
            "ambiguities": [item.to_dict() for item in self.ambiguities],
            "request_locale": self.request_locale,
            "preferred_locales": list(self.preferred_locales),
        }


@dataclass(frozen=True)
class EntityAliasProposal:
    canonical_ref: str
    alias: str
    locale: str = "und"
    status: EntityAliasProposalStatus = "proposed"
    action: EntityAliasAction = "alias.add"
    entity_kind: str | None = None
    normalized: str | None = None
    actor: str | None = None
    source: str = "named_entity_service"
    webspace_id: str | None = None
    request_id: str | None = None
    base_fingerprint: str | None = None
    reason: str | None = None
    conflicts: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_ref", _text(self.canonical_ref))
        object.__setattr__(self, "alias", _text(self.alias))
        object.__setattr__(self, "locale", _locale(self.locale))
        action = _text(self.action) or "alias.add"
        if action not in {"alias.add", "alias.remove", "alias.deprecate"}:
            action = "alias.add"
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "entity_kind", _text_or_none(self.entity_kind))
        object.__setattr__(self, "normalized", _text(self.normalized) or normalize_entity_label(self.alias))
        object.__setattr__(self, "actor", _text_or_none(self.actor))
        object.__setattr__(self, "source", _text(self.source) or "named_entity_service")
        object.__setattr__(self, "webspace_id", _text_or_none(self.webspace_id))
        object.__setattr__(self, "request_id", _text_or_none(self.request_id))
        object.__setattr__(self, "base_fingerprint", _text_or_none(self.base_fingerprint))
        object.__setattr__(self, "reason", _text_or_none(self.reason))
        object.__setattr__(self, "conflicts", tuple(_mapping(item) for item in self.conflicts))

    @property
    def ok(self) -> bool:
        return self.status in {"proposed", "noop"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "status": self.status,
            "canonical_ref": self.canonical_ref,
            "entity_kind": self.entity_kind,
            "alias": self.alias,
            "normalized": self.normalized,
            "locale": self.locale,
            "actor": self.actor,
            "source": self.source,
            "webspace_id": self.webspace_id,
            "request_id": self.request_id,
            "base_fingerprint": self.base_fingerprint,
            "reason": self.reason,
            "conflicts": [dict(item) for item in self.conflicts],
        }


@dataclass(frozen=True)
class EntityAliasApplyResult:
    proposal: EntityAliasProposal
    status: EntityAliasApplyStatus
    updated_record: NamedEntityRecord | None = None
    events: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status in {"applied", "noop"} and self.proposal.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "proposal": self.proposal.to_dict(),
            "updated_record": self.updated_record.to_dict() if self.updated_record is not None else None,
            "events": [dict(item) for item in self.events],
        }


def resolve_display_label(
    *,
    display_name: Any = None,
    registered_names: Any = None,
    observed_name: Any = None,
    draft_name: Any = None,
    fallback_label: Any = None,
) -> str:
    display = _text(display_name)
    if display:
        return display
    registered = _tuple_of_texts(registered_names)
    if registered:
        return registered[0]
    observed = _text(observed_name)
    if observed:
        return observed
    draft = _text(draft_name)
    if draft:
        return draft
    return _text(fallback_label)


def _confidence_for_match(match_type: EntityMatchType) -> float:
    if match_type in {"display_name", "alias"}:
        return 1.0
    if match_type == "registered_name":
        return 0.95
    if match_type == "observed_name":
        return 0.85
    if match_type == "draft_name":
        return 0.8
    return 0.55


def _entity_from_device(device: Mapping[str, Any]) -> NamedEntityRecord | None:
    ref = _text(device.get("ref"))
    if not ref:
        return None
    canonical_ref = canonical_device_ref(ref)
    kind_token = _text(device.get("kind"))
    identity = _mapping(device.get("identity"))
    policy = _mapping(device.get("policy"))
    observation = _mapping(device.get("observation"))
    diagnostics = _mapping(device.get("diagnostics"))
    if kind_token == "browser":
        entity_kind = "device.browser"
        fallback = _text(identity.get("browser_device_id")) or compatibility_device_ref(canonical_ref)
        draft_name = _browser_draft_name(identity, fallback=fallback)
    elif kind_token == "member":
        entity_kind = "device.member"
        fallback = _text(identity.get("node_id")) or compatibility_device_ref(canonical_ref)
        draft_name = None
    else:
        entity_kind = f"device.{kind_token}" if kind_token else "device"
        fallback = compatibility_device_ref(canonical_ref)
        draft_name = None
    display_name = _text_or_none(policy.get("display_name"))
    registered_names = _tuple_of_texts(identity.get("node_names"))
    observed_name = _text_or_none(identity.get("hostname"))
    aliases = _tuple_of_texts(policy.get("aliases"))
    labels = _tuple_of_labels(policy.get("labels"))
    status: EntityStatus = "confirmed" if display_name else "draft" if draft_name else "observed"
    updated_at = observation.get("last_seen_at") if isinstance(observation.get("last_seen_at"), (int, float)) else None
    return NamedEntityRecord(
        canonical_ref=canonical_ref,
        kind=entity_kind,
        display_name=display_name,
        registered_names=registered_names,
        observed_name=observed_name,
        draft_name=draft_name,
        aliases=aliases,
        labels=labels,
        fallback_label=fallback,
        scope={
            "node_id": identity.get("node_id"),
            "browser_device_id": identity.get("browser_device_id"),
            "last_webspace_id": observation.get("last_webspace_id"),
        },
        source="device_inventory",
        source_authority={
            "display_name": "access_links" if display_name else None,
            "registered_names": "device_inventory",
            "observed_name": observation.get("source") or "device_inventory",
            "draft_name": "named_entity_service.browser_draft" if draft_name else None,
            "aliases": "access_links" if aliases else None,
            "labels": "access_links" if labels else None,
            "diagnostics": diagnostics,
        },
        status=status,
        updated_at=updated_at,
    )


def _records_from_lookup_items(
    lookup_name: str,
    items: Any,
    *,
    webspace_id: str,
) -> list[NamedEntityRecord]:
    lookup_to_kind = {
        "modal_id": "modal",
        "app_id": "app",
        "scenario_id": "scenario",
        "webspace_id": "webspace",
        "skill_id": "skill",
    }
    entity_kind = lookup_to_kind.get(lookup_name)
    if not entity_kind:
        return []
    rows = list(items) if isinstance(items, list) else []
    records: list[NamedEntityRecord] = []
    for raw in rows:
        item = _mapping(raw)
        value = _text(item.get("value"))
        if not value:
            continue
        labels = _tuple_of_texts(item.get("labels"))
        display_name = labels[0] if labels else value
        registered_names = (value,) if normalize_entity_label(display_name) != normalize_entity_label(value) else ()
        records.append(
            NamedEntityRecord(
                canonical_ref=f"{entity_kind}:{value}",
                kind=entity_kind,
                display_name=display_name,
                registered_names=registered_names,
                aliases=labels[1:],
                fallback_label=value,
                scope={"webspace_id": webspace_id},
                source="nlu_lookup_tables",
                source_authority={
                    "lookup": lookup_name,
                    "sources": list(item.get("sources") or []),
                },
                status="confirmed",
            )
        )
    return records


def _entity_from_current_subnet() -> NamedEntityRecord | None:
    try:
        from adaos.services.node_config import load_config
        from adaos.services.subnet_alias import load_subnet_alias
    except Exception:
        return None
    try:
        conf = load_config()
    except Exception:
        return None
    subnet_id = _text(getattr(conf, "subnet_id", "") or getattr(conf, "subnet_id_value", ""))
    if not subnet_id:
        return None
    alias = _text_or_none(load_subnet_alias(subnet_id=subnet_id))
    return NamedEntityRecord(
        canonical_ref=f"assistant:{subnet_id}",
        kind="assistant",
        display_name=alias,
        registered_names=(alias,) if alias else (),
        fallback_label=subnet_id,
        scope={
            "subnet_id": subnet_id,
            "node_id": _text(getattr(conf, "node_id", "") or getattr(conf, "node_id_value", "")) or None,
        },
        source="node_config.subnet",
        source_authority={
            "display_name": ".adaos/node.yaml: subnet.names" if alias else None,
            "registered_names": ".adaos/node.yaml: subnet.names" if alias else None,
            "fallback": "node_config.subnet_id",
        },
        status="confirmed" if alias else "observed",
    )


def _proposal_from_any(value: EntityAliasProposal | Mapping[str, Any]) -> EntityAliasProposal:
    if isinstance(value, EntityAliasProposal):
        return value
    payload = _mapping(value)
    return EntityAliasProposal(
        canonical_ref=payload.get("canonical_ref") or payload.get("entity_ref"),
        entity_kind=payload.get("entity_kind") or payload.get("kind"),
        alias=payload.get("alias"),
        locale=payload.get("locale") or "und",
        status=payload.get("status") or "proposed",
        action=payload.get("action") or "alias.add",
        normalized=payload.get("normalized"),
        actor=payload.get("actor"),
        source=payload.get("source") or "named_entity_service",
        webspace_id=payload.get("webspace_id"),
        request_id=payload.get("request_id"),
        base_fingerprint=payload.get("base_fingerprint"),
        reason=payload.get("reason"),
        conflicts=tuple(payload.get("conflicts") or ()),
    )


def _event_envelope(topic: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"topic": topic, "payload": dict(payload)}


def _device_event_scope(kind: str, entry_id: str, current: Mapping[str, Any]) -> dict[str, Any]:
    webspace_id = _text(current.get("last_webspace_id"))
    return {
        "device_id": _text(entry_id),
        "link_kind": _text(kind),
        **({"webspace_id": webspace_id} if webspace_id else {}),
    }


def _browser_draft_from_registry_entry(entry_id: str, entry: Mapping[str, Any]) -> str | None:
    if not any(_text(entry.get(key)) for key in ("browser_family", "os_name", "form_factor")):
        return None
    identity = {
        "browser_family": entry.get("browser_family"),
        "os_name": entry.get("os_name"),
        "form_factor": entry.get("form_factor"),
        "browser_device_id": entry_id,
    }
    return _browser_draft_name(identity, fallback=entry_id)


def device_entity_lifecycle_event_envelopes(
    *,
    kind: str,
    entry_id: str,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    source: str,
    reason: str,
) -> tuple[Mapping[str, Any], ...]:
    link_kind = "member" if _text(kind) == "member" else "browser"
    token = _text(entry_id)
    if not token:
        return ()
    previous_view = _mapping(previous)
    current_view = _mapping(current)
    entity_ref = f"device:{link_kind}:{token}"
    entity_kind = f"device.{link_kind}"
    scope = _device_event_scope(link_kind, token, current_view)
    events: list[Mapping[str, Any]] = []

    previous_display = _text(previous_view.get("display_name"))
    current_display = _text(current_view.get("display_name"))
    if current_display and previous_display != current_display:
        payload = entity_event_payload(
            entity_ref=entity_ref,
            entity_kind=entity_kind,
            source=source,
            scope=scope,
            previous={"display_name": previous_display or None},
            current={"display_name": current_display},
            reason=reason or "display_name.changed",
        )
        events.append(_event_envelope(ENTITY_DISPLAY_NAME_CHANGED, payload))

    if link_kind == "member":
        observed_changed = (
            _text(previous_view.get("hostname")) != _text(current_view.get("hostname"))
            or list(previous_view.get("node_names") or []) != list(current_view.get("node_names") or [])
        )
        current_hostname = _text(current_view.get("hostname"))
        current_node_names = [str(item) for item in list(current_view.get("node_names") or []) if str(item).strip()]
        if observed_changed and (current_hostname or current_node_names):
            payload = entity_event_payload(
                entity_ref=entity_ref,
                entity_kind=entity_kind,
                source=source,
                scope=scope,
                previous={
                    "observed_name": _text(previous_view.get("hostname")) or None,
                    "registered_names": list(previous_view.get("node_names") or []),
                },
                current={
                    "observed_name": current_hostname or None,
                    "registered_names": current_node_names,
                },
                reason=reason or "entity_observed",
            )
            events.append(_event_envelope(ENTITY_OBSERVED, payload))
    else:
        observed_keys = ("browser_family", "os_name", "form_factor", "user_agent")
        observed_changed = any(_text(previous_view.get(key)) != _text(current_view.get(key)) for key in observed_keys)
        observed_current = {key: _text(current_view.get(key)) for key in observed_keys if _text(current_view.get(key))}
        if observed_changed and observed_current:
            payload = entity_event_payload(
                entity_ref=entity_ref,
                entity_kind=entity_kind,
                source=source,
                scope=scope,
                previous={key: _text(previous_view.get(key)) or None for key in observed_keys},
                current=observed_current,
                reason=reason or "entity_observed",
            )
            events.append(_event_envelope(ENTITY_OBSERVED, payload))

        previous_draft = _browser_draft_from_registry_entry(token, previous_view)
        current_draft = _browser_draft_from_registry_entry(token, current_view)
        if current_draft and previous_draft != current_draft:
            payload = entity_event_payload(
                entity_ref=entity_ref,
                entity_kind=entity_kind,
                source=source,
                scope=scope,
                previous={"draft_name": previous_draft},
                current={
                    "draft_name": current_draft,
                    "basis": {
                        key: _text(current_view.get(key))
                        for key in ("browser_family", "os_name", "form_factor")
                        if _text(current_view.get(key))
                    },
                },
                reason=reason or "draft_name.suggested",
            )
            events.append(_event_envelope(ENTITY_DRAFT_NAME_SUGGESTED, payload))

    return tuple(events)


def _alias_label_matches(label: EntityLabel, *, normalized: str, locale: str) -> bool:
    return (
        label.role == "alias"
        and normalize_entity_label(label.text) == normalized
        and label.locale.casefold() == _locale(locale).casefold()
    )


class NamedEntityService:
    def __init__(
        self,
        *,
        device_inventory_service: Any | None = None,
        lookup_payload_provider: Any | None = None,
        default_webspace_id: str = "desktop",
        static_entities: Iterable[NamedEntityRecord] | None = None,
        include_runtime_subnet_entity: bool | None = None,
    ) -> None:
        self._device_inventory_service = device_inventory_service
        self._lookup_payload_provider = lookup_payload_provider
        self._default_webspace_id = _text(default_webspace_id) or "desktop"
        self._static_entities = tuple(static_entities or ())
        if include_runtime_subnet_entity is None:
            include_runtime_subnet_entity = (
                device_inventory_service is None
                and lookup_payload_provider is None
                and not self._static_entities
            )
        self._include_runtime_subnet_entity = bool(include_runtime_subnet_entity)

    def list_entities(
        self,
        *,
        kind: str | None = None,
        webspace_id: str | None = None,
    ) -> list[NamedEntityRecord]:
        records: list[NamedEntityRecord] = list(self._static_entities)
        if self._include_runtime_subnet_entity:
            subnet_entity = _entity_from_current_subnet()
            if subnet_entity is not None:
                records.append(subnet_entity)
        records.extend(self._list_device_entities())
        records.extend(self._list_lookup_entities(webspace_id=webspace_id))
        if kind:
            wanted = _text(kind)
            records = [item for item in records if item.kind == wanted]
        records.sort(key=lambda item: (item.kind, item.display_label.casefold(), item.canonical_ref))
        return records

    def resolve_text(
        self,
        text: str,
        *,
        kind: str | None = None,
        include_fallback: bool = False,
        webspace_id: str | None = None,
        request_locale: str | None = None,
        preferred_locales: Iterable[str] | None = None,
    ) -> EntityResolutionResult:
        raw_text = _text(text)
        normalized_text = normalize_entity_label(raw_text)
        locale_order = _preferred_locales(request_locale=request_locale, preferred_locales=preferred_locales)
        records = self.list_entities(kind=kind, webspace_id=webspace_id)
        matches_by_span: dict[tuple[int, int, str], list[EntityResolutionMatch]] = {}
        for record in records:
            labels = sorted(
                record.label_records(include_fallback=include_fallback),
                key=lambda item: (
                    -_locale_match_score(item.locale, locale_order),
                    -(_confidence_for_match(item.match_type)),
                    item.text.casefold(),
                ),
            )
            for label in labels:
                pattern = re.compile(rf"(?<!\w){re.escape(label.text)}(?!\w)", re.IGNORECASE)
                for match in pattern.finditer(raw_text):
                    key = (match.start(), match.end(), normalize_entity_label(match.group(0)))
                    confidence = _confidence_for_match(label.match_type) * _locale_match_score(label.locale, locale_order)
                    matches_by_span.setdefault(key, []).append(
                        EntityResolutionMatch(
                            canonical_ref=record.canonical_ref,
                            kind=record.kind,
                            text=match.group(0),
                            normalized=key[2],
                            start=match.start(),
                            end=match.end(),
                            confidence=round(confidence, 4),
                            match_type=label.match_type,
                            display_label=record.display_label,
                            locale=label.locale,
                        )
                    )

        resolved: list[EntityResolutionMatch] = []
        ambiguities: list[EntityResolutionAmbiguity] = []
        for (_start, _end, normalized), candidates in sorted(matches_by_span.items()):
            unique: dict[str, EntityResolutionMatch] = {}
            for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
                unique.setdefault(candidate.canonical_ref, candidate)
            deduped = tuple(unique.values())
            if len(deduped) == 1:
                resolved.append(deduped[0])
            else:
                ambiguities.append(
                    EntityResolutionAmbiguity(
                        text=deduped[0].text,
                        normalized=normalized,
                        candidates=deduped,
                    )
                )
        return EntityResolutionResult(
            raw_text=raw_text,
            normalized_text=normalized_text,
            resolved_entities=tuple(resolved),
            ambiguities=tuple(ambiguities),
            request_locale=_text_or_none(request_locale),
            preferred_locales=locale_order,
        )

    def propose_alias_add(
        self,
        *,
        canonical_ref: str,
        alias: str,
        locale: str | None = None,
        kind: str | None = None,
        webspace_id: str | None = None,
        actor: str | None = None,
        source: str = "named_entity_service",
        request_id: str | None = None,
        base_fingerprint: str | None = None,
    ) -> EntityAliasProposal:
        alias_text = _text(alias)
        normalized = normalize_entity_label(alias_text)
        effective_locale = _locale(locale)
        expected_fingerprint = _text_or_none(base_fingerprint)
        if not alias_text or not normalized:
            return EntityAliasProposal(
                canonical_ref=canonical_ref,
                entity_kind=kind,
                alias=alias_text,
                locale=effective_locale,
                status="invalid",
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="alias_empty",
            )

        record = self._find_entity(canonical_ref=canonical_ref, kind=kind, webspace_id=webspace_id)
        if record is None:
            return EntityAliasProposal(
                canonical_ref=canonical_ref,
                entity_kind=kind,
                alias=alias_text,
                locale=effective_locale,
                status="not_found",
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="entity_not_found",
            )

        if expected_fingerprint and record.fingerprint != expected_fingerprint:
            return EntityAliasProposal(
                canonical_ref=record.canonical_ref,
                entity_kind=record.kind,
                alias=alias_text,
                locale=effective_locale,
                status="stale",
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="base_fingerprint_mismatch",
                conflicts=(
                    {
                        "canonical_ref": record.canonical_ref,
                        "kind": record.kind,
                        "display_label": record.display_label,
                        "base_fingerprint": expected_fingerprint,
                        "current_fingerprint": record.fingerprint,
                    },
                ),
            )

        if self._record_has_label(record, normalized=normalized, locale=effective_locale):
            return EntityAliasProposal(
                canonical_ref=record.canonical_ref,
                entity_kind=record.kind,
                alias=alias_text,
                locale=effective_locale,
                status="noop",
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="alias_already_registered",
            )

        conflicts = self._alias_conflicts(
            alias=alias_text,
            locale=effective_locale,
            canonical_ref=record.canonical_ref,
            kind=kind,
            webspace_id=webspace_id,
        )
        if conflicts:
            return EntityAliasProposal(
                canonical_ref=record.canonical_ref,
                entity_kind=record.kind,
                alias=alias_text,
                locale=effective_locale,
                status="conflict",
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="alias_conflict",
                conflicts=tuple(conflicts),
            )

        return EntityAliasProposal(
            canonical_ref=record.canonical_ref,
            entity_kind=record.kind,
            alias=alias_text,
            locale=effective_locale,
            status="proposed",
            normalized=normalized,
            actor=actor,
            source=source,
            webspace_id=webspace_id,
            request_id=request_id,
            base_fingerprint=expected_fingerprint,
            reason="alias_available",
        )

    def propose_alias_remove(
        self,
        *,
        canonical_ref: str,
        alias: str,
        locale: str | None = None,
        kind: str | None = None,
        webspace_id: str | None = None,
        actor: str | None = None,
        source: str = "named_entity_service",
        request_id: str | None = None,
        base_fingerprint: str | None = None,
    ) -> EntityAliasProposal:
        return self._propose_alias_state_change(
            "alias.remove",
            canonical_ref=canonical_ref,
            alias=alias,
            locale=locale,
            kind=kind,
            webspace_id=webspace_id,
            actor=actor,
            source=source,
            request_id=request_id,
            base_fingerprint=base_fingerprint,
        )

    def propose_alias_deprecate(
        self,
        *,
        canonical_ref: str,
        alias: str,
        locale: str | None = None,
        kind: str | None = None,
        webspace_id: str | None = None,
        actor: str | None = None,
        source: str = "named_entity_service",
        request_id: str | None = None,
        base_fingerprint: str | None = None,
    ) -> EntityAliasProposal:
        return self._propose_alias_state_change(
            "alias.deprecate",
            canonical_ref=canonical_ref,
            alias=alias,
            locale=locale,
            kind=kind,
            webspace_id=webspace_id,
            actor=actor,
            source=source,
            request_id=request_id,
            base_fingerprint=base_fingerprint,
        )

    def _propose_alias_state_change(
        self,
        action: EntityAliasAction,
        *,
        canonical_ref: str,
        alias: str,
        locale: str | None,
        kind: str | None,
        webspace_id: str | None,
        actor: str | None,
        source: str,
        request_id: str | None,
        base_fingerprint: str | None,
    ) -> EntityAliasProposal:
        alias_text = _text(alias)
        normalized = normalize_entity_label(alias_text)
        effective_locale = _locale(locale)
        expected_fingerprint = _text_or_none(base_fingerprint)
        if not alias_text or not normalized:
            return EntityAliasProposal(
                canonical_ref=canonical_ref,
                entity_kind=kind,
                alias=alias_text,
                locale=effective_locale,
                status="invalid",
                action=action,
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="alias_empty",
            )

        record = self._find_entity(canonical_ref=canonical_ref, kind=kind, webspace_id=webspace_id)
        if record is None:
            return EntityAliasProposal(
                canonical_ref=canonical_ref,
                entity_kind=kind,
                alias=alias_text,
                locale=effective_locale,
                status="not_found",
                action=action,
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="entity_not_found",
            )

        if expected_fingerprint and record.fingerprint != expected_fingerprint:
            return EntityAliasProposal(
                canonical_ref=record.canonical_ref,
                entity_kind=record.kind,
                alias=alias_text,
                locale=effective_locale,
                status="stale",
                action=action,
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason="base_fingerprint_mismatch",
                conflicts=(
                    {
                        "canonical_ref": record.canonical_ref,
                        "kind": record.kind,
                        "display_label": record.display_label,
                        "base_fingerprint": expected_fingerprint,
                        "current_fingerprint": record.fingerprint,
                    },
                ),
            )

        include_deprecated = action == "alias.remove"
        if not self._record_has_alias(record, normalized=normalized, locale=effective_locale, include_deprecated=include_deprecated):
            reason = (
                "alias_already_deprecated"
                if action == "alias.deprecate"
                and self._record_has_alias(record, normalized=normalized, locale=effective_locale, include_deprecated=True)
                else "alias_not_registered"
            )
            return EntityAliasProposal(
                canonical_ref=record.canonical_ref,
                entity_kind=record.kind,
                alias=alias_text,
                locale=effective_locale,
                status="noop",
                action=action,
                normalized=normalized,
                actor=actor,
                source=source,
                webspace_id=webspace_id,
                request_id=request_id,
                base_fingerprint=expected_fingerprint,
                reason=reason,
            )

        return EntityAliasProposal(
            canonical_ref=record.canonical_ref,
            entity_kind=record.kind,
            alias=alias_text,
            locale=effective_locale,
            status="proposed",
            action=action,
            normalized=normalized,
            actor=actor,
            source=source,
            webspace_id=webspace_id,
            request_id=request_id,
            base_fingerprint=expected_fingerprint,
            reason="alias_registered",
        )

    def apply_alias_add(self, proposal: EntityAliasProposal | Mapping[str, Any]) -> EntityAliasApplyResult:
        proposed = replace(_proposal_from_any(proposal), action="alias.add")
        if proposed.status == "noop":
            return EntityAliasApplyResult(proposal=proposed, status="noop")

        if proposed.status in {"conflict", "stale"}:
            event_payload = entity_event_payload(
                entity_ref=proposed.canonical_ref,
                entity_kind=proposed.entity_kind or "",
                source=proposed.source,
                actor=proposed.actor,
                current={
                    "action": proposed.action,
                    "alias": proposed.alias,
                    "normalized": proposed.normalized,
                    "locale": proposed.locale,
                    "conflicts": [dict(item) for item in proposed.conflicts],
                    "base_fingerprint": proposed.base_fingerprint,
                },
                reason=proposed.reason or "alias_conflict",
                request_id=proposed.request_id,
                locale=proposed.locale,
            )
            return EntityAliasApplyResult(
                proposal=proposed,
                status=proposed.status,
                events=(_event_envelope(ENTITY_ALIAS_CONFLICT_DETECTED, event_payload),),
            )

        if proposed.status != "proposed":
            return EntityAliasApplyResult(proposal=proposed, status=proposed.status)

        record = self._find_entity(
            canonical_ref=proposed.canonical_ref,
            kind=proposed.entity_kind,
            webspace_id=proposed.webspace_id,
        )
        if record is None:
            missing = replace(proposed, status="not_found", reason="entity_not_found")
            return EntityAliasApplyResult(proposal=missing, status="not_found")

        conflicts = self._alias_conflicts(
            alias=proposed.alias,
            locale=proposed.locale,
            canonical_ref=record.canonical_ref,
            kind=proposed.entity_kind,
            webspace_id=proposed.webspace_id,
        )
        if conflicts:
            conflicted = replace(proposed, status="conflict", reason="alias_conflict", conflicts=tuple(conflicts))
            return self.apply_alias_add(conflicted)

        label = EntityLabel(
            text=proposed.alias,
            locale=proposed.locale,
            role="alias",
            status="confirmed",
            source=proposed.source,
        )
        labels = tuple(
            item
            for item in record.labels
            if not _alias_label_matches(
                item,
                normalized=proposed.normalized or normalize_entity_label(proposed.alias),
                locale=proposed.locale,
            )
        )
        updated = replace(record, labels=labels + (label,), status="confirmed")
        alias_event = entity_event_payload(
            entity_ref=record.canonical_ref,
            entity_kind=record.kind,
            source=proposed.source,
            actor=proposed.actor,
            previous={"labels": [item.to_dict() for item in record.label_records(include_fallback=False)]},
            current={
                "label": label.to_dict(),
                "display_label": updated.display_label,
                "base_fingerprint": proposed.base_fingerprint,
                "previous_fingerprint": record.fingerprint,
                "current_fingerprint": updated.fingerprint,
            },
            reason=proposed.reason or "alias_added",
            request_id=proposed.request_id,
            locale=proposed.locale,
        )
        changed_event = entity_event_payload(
            entity_ref=record.canonical_ref,
            entity_kind=record.kind,
            source=proposed.source,
            actor=proposed.actor,
            previous={"fingerprint": record.fingerprint, "fingerprint_basis": "labels"},
            current={"fingerprint": updated.fingerprint, "changed": True, "reason": "alias_added"},
            reason="alias_added",
            request_id=proposed.request_id,
            locale=proposed.locale,
        )
        return EntityAliasApplyResult(
            proposal=proposed,
            status="applied",
            updated_record=updated,
            events=(
                _event_envelope(ENTITY_ALIAS_ADDED, alias_event),
                _event_envelope(ENTITY_REGISTRY_CHANGED, changed_event),
            ),
        )

    def apply_alias_remove(self, proposal: EntityAliasProposal | Mapping[str, Any]) -> EntityAliasApplyResult:
        return self._apply_alias_state_change(
            proposal,
            action="alias.remove",
            event_topic=ENTITY_ALIAS_REMOVED,
            changed_reason="alias_removed",
        )

    def apply_alias_deprecate(self, proposal: EntityAliasProposal | Mapping[str, Any]) -> EntityAliasApplyResult:
        return self._apply_alias_state_change(
            proposal,
            action="alias.deprecate",
            event_topic=ENTITY_ALIAS_DEPRECATED,
            changed_reason="alias_deprecated",
        )

    def _apply_alias_state_change(
        self,
        proposal: EntityAliasProposal | Mapping[str, Any],
        *,
        action: EntityAliasAction,
        event_topic: str,
        changed_reason: str,
    ) -> EntityAliasApplyResult:
        proposed = replace(_proposal_from_any(proposal), action=action)
        if proposed.status == "noop":
            return EntityAliasApplyResult(proposal=proposed, status="noop")

        if proposed.status in {"conflict", "stale"}:
            event_payload = entity_event_payload(
                entity_ref=proposed.canonical_ref,
                entity_kind=proposed.entity_kind or "",
                source=proposed.source,
                actor=proposed.actor,
                current={
                    "action": action,
                    "alias": proposed.alias,
                    "normalized": proposed.normalized,
                    "locale": proposed.locale,
                    "conflicts": [dict(item) for item in proposed.conflicts],
                    "base_fingerprint": proposed.base_fingerprint,
                },
                reason=proposed.reason or "alias_state_conflict",
                request_id=proposed.request_id,
                locale=proposed.locale,
            )
            return EntityAliasApplyResult(
                proposal=proposed,
                status=proposed.status,
                events=(_event_envelope(ENTITY_ALIAS_CONFLICT_DETECTED, event_payload),),
            )

        if proposed.status != "proposed":
            return EntityAliasApplyResult(proposal=proposed, status=proposed.status)

        record = self._find_entity(
            canonical_ref=proposed.canonical_ref,
            kind=proposed.entity_kind,
            webspace_id=proposed.webspace_id,
        )
        if record is None:
            missing = replace(proposed, status="not_found", reason="entity_not_found")
            return EntityAliasApplyResult(proposal=missing, status="not_found")

        include_deprecated = action == "alias.remove"
        if not self._record_has_alias(
            record,
            normalized=proposed.normalized or normalize_entity_label(proposed.alias),
            locale=proposed.locale,
            include_deprecated=include_deprecated,
        ):
            missing = replace(proposed, status="noop", reason="alias_not_registered")
            return EntityAliasApplyResult(proposal=missing, status="noop")

        updated = self._updated_record_for_alias_state_change(record, proposed, action=action)
        alias_event = entity_event_payload(
            entity_ref=record.canonical_ref,
            entity_kind=record.kind,
            source=proposed.source,
            actor=proposed.actor,
            previous={"labels": [item.to_dict() for item in record.label_records(include_fallback=False)]},
            current={
                "action": action,
                "alias": proposed.alias,
                "normalized": proposed.normalized,
                "locale": proposed.locale,
                "display_label": updated.display_label,
                "base_fingerprint": proposed.base_fingerprint,
                "previous_fingerprint": record.fingerprint,
                "current_fingerprint": updated.fingerprint,
            },
            reason=changed_reason,
            request_id=proposed.request_id,
            locale=proposed.locale,
        )
        changed_event = entity_event_payload(
            entity_ref=record.canonical_ref,
            entity_kind=record.kind,
            source=proposed.source,
            actor=proposed.actor,
            previous={"fingerprint": record.fingerprint, "fingerprint_basis": "labels"},
            current={"fingerprint": updated.fingerprint, "changed": True, "reason": changed_reason},
            reason=changed_reason,
            request_id=proposed.request_id,
            locale=proposed.locale,
        )
        return EntityAliasApplyResult(
            proposal=proposed,
            status="applied",
            updated_record=updated,
            events=(
                _event_envelope(event_topic, alias_event),
                _event_envelope(ENTITY_REGISTRY_CHANGED, changed_event),
            ),
        )

    def _find_entity(
        self,
        *,
        canonical_ref: str,
        kind: str | None = None,
        webspace_id: str | None = None,
    ) -> NamedEntityRecord | None:
        wanted_ref = _text(canonical_ref)
        if not wanted_ref:
            return None
        for record in self.list_entities(kind=kind, webspace_id=webspace_id):
            if record.canonical_ref == wanted_ref:
                return record
        return None

    def _record_has_label(self, record: NamedEntityRecord, *, normalized: str, locale: str) -> bool:
        folded_locale = _locale(locale).casefold()
        for label in record.label_records(include_fallback=False):
            if label.status == "deprecated":
                continue
            if normalize_entity_label(label.text) == normalized and label.locale.casefold() == folded_locale:
                return True
        return False

    def _record_has_alias(
        self,
        record: NamedEntityRecord,
        *,
        normalized: str,
        locale: str,
        include_deprecated: bool = False,
    ) -> bool:
        folded_locale = _locale(locale).casefold()
        if folded_locale == "und":
            for alias in record.aliases:
                if normalize_entity_label(alias) == normalized:
                    return True
        for label in record.labels:
            if not _alias_label_matches(label, normalized=normalized, locale=locale):
                continue
            if include_deprecated or label.status != "deprecated":
                return True
        return False

    def _updated_record_for_alias_state_change(
        self,
        record: NamedEntityRecord,
        proposal: EntityAliasProposal,
        *,
        action: EntityAliasAction,
    ) -> NamedEntityRecord:
        normalized = proposal.normalized or normalize_entity_label(proposal.alias)
        folded_locale = proposal.locale.casefold()
        aliases = tuple(
            alias
            for alias in record.aliases
            if not (folded_locale == "und" and normalize_entity_label(alias) == normalized)
        )
        labels: list[EntityLabel] = []
        matched_legacy_alias = len(aliases) != len(record.aliases)
        for label in record.labels:
            if not _alias_label_matches(label, normalized=normalized, locale=proposal.locale):
                labels.append(label)
                continue
            if action == "alias.remove":
                continue
            labels.append(
                replace(
                    label,
                    status="deprecated",
                    source=label.source or proposal.source,
                )
            )
        if action == "alias.deprecate" and matched_legacy_alias:
            labels.append(
                EntityLabel(
                    text=proposal.alias,
                    locale=proposal.locale,
                    role="alias",
                    status="deprecated",
                    source=proposal.source,
                )
            )
        return replace(record, aliases=aliases, labels=tuple(labels), status="confirmed")

    def _alias_conflicts(
        self,
        *,
        alias: str,
        locale: str,
        canonical_ref: str,
        kind: str | None = None,
        webspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized = normalize_entity_label(alias)
        folded_locale = _locale(locale).casefold()
        out: list[dict[str, Any]] = []
        for record in self.list_entities(kind=kind, webspace_id=webspace_id):
            if record.canonical_ref == _text(canonical_ref):
                continue
            for label in record.label_records(include_fallback=False):
                if label.locale.casefold() != folded_locale:
                    continue
                if normalize_entity_label(label.text) != normalized:
                    continue
                out.append(
                    {
                        "canonical_ref": record.canonical_ref,
                        "kind": record.kind,
                        "display_label": record.display_label,
                        "label": label.to_dict(),
                    }
                )
        out.sort(key=lambda item: (str(item.get("kind") or ""), str(item.get("canonical_ref") or "")))
        return out

    def _list_device_entities(self) -> list[NamedEntityRecord]:
        try:
            service = self._device_inventory_service or device_inventory.get_device_inventory_service()
            devices = list(service.list_devices() or [])
        except Exception:
            devices = []
        records: list[NamedEntityRecord] = []
        for device in devices:
            record = _entity_from_device(_mapping(device))
            if record is not None:
                records.append(record)
        return records

    def _lookup_payload(self, *, webspace_id: str | None = None) -> Mapping[str, Any]:
        webspace = _text(webspace_id) or self._default_webspace_id
        provider = self._lookup_payload_provider
        if provider is not None:
            try:
                payload = provider(webspace_id=webspace)
            except TypeError:
                payload = provider()
            except Exception:
                return {}
            return payload if isinstance(payload, Mapping) else {}
        try:
            from adaos.services.agent_context import get_ctx
            from adaos.services.nlu_lookup_tables import collect_desktop_lookup_tables

            payload = collect_desktop_lookup_tables(get_ctx(), webspace_id=webspace)
        except Exception:
            return {}
        return payload if isinstance(payload, Mapping) else {}

    def _list_lookup_entities(self, *, webspace_id: str | None = None) -> list[NamedEntityRecord]:
        payload = self._lookup_payload(webspace_id=webspace_id)
        lookups = payload.get("lookups") if isinstance(payload.get("lookups"), Mapping) else {}
        webspace = _text(payload.get("webspace_id")) or _text(webspace_id) or self._default_webspace_id
        records: list[NamedEntityRecord] = []
        records.extend(_records_from_lookup_items("modal_id", lookups.get("modal_id"), webspace_id=webspace))
        records.extend(_records_from_lookup_items("app_id", lookups.get("app_id"), webspace_id=webspace))
        records.extend(_records_from_lookup_items("scenario_id", lookups.get("scenario_id"), webspace_id=webspace))
        records.extend(_records_from_lookup_items("webspace_id", lookups.get("webspace_id"), webspace_id=webspace))
        records.extend(_records_from_lookup_items("skill_id", lookups.get("skill_id"), webspace_id=webspace))
        return records


_SERVICE: NamedEntityService | None = None


def get_named_entity_service() -> NamedEntityService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = NamedEntityService()
    return _SERVICE


def list_entities(*, kind: str | None = None, webspace_id: str | None = None) -> list[dict[str, Any]]:
    return [
        item.to_dict()
        for item in get_named_entity_service().list_entities(kind=kind, webspace_id=webspace_id)
    ]


def resolve_text(
    text: str,
    *,
    kind: str | None = None,
    include_fallback: bool = False,
    webspace_id: str | None = None,
    request_locale: str | None = None,
    preferred_locales: Iterable[str] | None = None,
) -> dict[str, Any]:
    return get_named_entity_service().resolve_text(
        text,
        kind=kind,
        include_fallback=include_fallback,
        webspace_id=webspace_id,
        request_locale=request_locale,
        preferred_locales=preferred_locales,
    ).to_dict()


def propose_alias_add(
    *,
    canonical_ref: str,
    alias: str,
    locale: str | None = None,
    kind: str | None = None,
    webspace_id: str | None = None,
    actor: str | None = None,
    source: str = "named_entity_service",
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return get_named_entity_service().propose_alias_add(
        canonical_ref=canonical_ref,
        alias=alias,
        locale=locale,
        kind=kind,
        webspace_id=webspace_id,
        actor=actor,
        source=source,
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    ).to_dict()


def apply_alias_add(proposal: EntityAliasProposal | Mapping[str, Any]) -> dict[str, Any]:
    return get_named_entity_service().apply_alias_add(proposal).to_dict()


def propose_alias_remove(
    *,
    canonical_ref: str,
    alias: str,
    locale: str | None = None,
    kind: str | None = None,
    webspace_id: str | None = None,
    actor: str | None = None,
    source: str = "named_entity_service",
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return get_named_entity_service().propose_alias_remove(
        canonical_ref=canonical_ref,
        alias=alias,
        locale=locale,
        kind=kind,
        webspace_id=webspace_id,
        actor=actor,
        source=source,
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    ).to_dict()


def apply_alias_remove(proposal: EntityAliasProposal | Mapping[str, Any]) -> dict[str, Any]:
    return get_named_entity_service().apply_alias_remove(proposal).to_dict()


def propose_alias_deprecate(
    *,
    canonical_ref: str,
    alias: str,
    locale: str | None = None,
    kind: str | None = None,
    webspace_id: str | None = None,
    actor: str | None = None,
    source: str = "named_entity_service",
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return get_named_entity_service().propose_alias_deprecate(
        canonical_ref=canonical_ref,
        alias=alias,
        locale=locale,
        kind=kind,
        webspace_id=webspace_id,
        actor=actor,
        source=source,
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    ).to_dict()


def apply_alias_deprecate(proposal: EntityAliasProposal | Mapping[str, Any]) -> dict[str, Any]:
    return get_named_entity_service().apply_alias_deprecate(proposal).to_dict()


def _registry_conflicts(records: Iterable[NamedEntityRecord]) -> list[dict[str, Any]]:
    by_label: dict[str, dict[str, Any]] = {}
    for record in records:
        for label in record.label_records(include_fallback=False):
            normalized = normalize_entity_label(label.text)
            if not normalized:
                continue
            bucket_key = f"{label.locale.casefold()}:{normalized}"
            bucket = by_label.setdefault(
                bucket_key,
                {
                    "label": label.text,
                    "locale": label.locale,
                    "normalized": normalized,
                    "candidates": {},
                },
            )
            candidates = bucket["candidates"]
            if record.canonical_ref not in candidates:
                candidates[record.canonical_ref] = {
                    "canonical_ref": record.canonical_ref,
                    "kind": record.kind,
                    "display_label": record.display_label,
                    "locale": label.locale,
                    "match_types": [],
                }
            candidate = candidates[record.canonical_ref]
            if label.match_type not in candidate["match_types"]:
                candidate["match_types"].append(label.match_type)
    conflicts: list[dict[str, Any]] = []
    for bucket in by_label.values():
        candidates = list(bucket["candidates"].values())
        if len(candidates) < 2:
            continue
        candidates.sort(key=lambda item: (str(item.get("kind") or ""), str(item.get("canonical_ref") or "")))
        conflicts.append(
            {
                "label": bucket["label"],
                "locale": bucket["locale"],
                "normalized": bucket["normalized"],
                "candidate_count": len(candidates),
                "candidates": candidates,
            }
        )
    conflicts.sort(
        key=lambda item: (
            str(item.get("locale") or ""),
            str(item.get("normalized") or ""),
            int(item.get("candidate_count") or 0),
        )
    )
    return conflicts


def named_entity_registry_access_metadata() -> dict[str, Any]:
    return {
        "read_only": True,
        "owner": "core:named_entities",
        "write_policy": "governed-alias-tools-only",
        "privacy": {
            "payload_class": "compact_descriptor",
            "contains_secret_data": False,
            "contains_skill_payload": False,
            "exposed_fields": [
                "canonical_ref",
                "kind",
                "display_label",
                "labels",
                "status",
                "scope",
                "source",
                "fingerprint",
            ],
        },
    }


def compact_registry_payload(
    *,
    kind: str | None = None,
    webspace_id: str | None = None,
    service: NamedEntityService | None = None,
) -> dict[str, Any]:
    webspace = _text(webspace_id) or "desktop"
    records = (service or get_named_entity_service()).list_entities(kind=kind, webspace_id=webspace)
    items = [
        {
            "canonical_ref": item.canonical_ref,
            "kind": item.kind,
            "display_label": item.display_label,
            "labels": [label.to_dict() for label in item.label_records(include_fallback=False)],
            "status": item.status,
            "scope": dict(item.scope),
            "source": item.source,
            "fingerprint": item.fingerprint,
        }
        for item in records
    ]
    conflicts = _registry_conflicts(records)
    fingerprint = hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "schema": NAMED_ENTITY_REGISTRY_SCHEMA,
        "version": 1,
        "webspace_id": webspace,
        "yjs_path": NAMED_ENTITY_REGISTRY_YJS_PATH,
        "access": named_entity_registry_access_metadata(),
        "items": items,
        "summary": {
            "count": len(items),
            "fingerprint": fingerprint,
            "updated_at": time.time(),
            "conflict_count": len(conflicts),
        },
        "conflicts": conflicts,
    }


def entity_event_payload(
    *,
    entity_ref: str,
    entity_kind: str,
    source: str,
    scope: Mapping[str, Any] | None = None,
    actor: str | None = None,
    previous: Mapping[str, Any] | None = None,
    current: Mapping[str, Any] | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    locale: str | None = None,
    preferred_locales: Iterable[str] | None = None,
) -> dict[str, Any]:
    return {
        "entity_ref": _text(entity_ref),
        "entity_kind": _text(entity_kind),
        "scope": _mapping(scope),
        "source": _text(source),
        "actor": _text_or_none(actor),
        "previous": _mapping(previous),
        "current": _mapping(current),
        "reason": _text_or_none(reason),
        "request_id": _text_or_none(request_id),
        "locale": _text_or_none(locale),
        "preferred_locales": list(
            _preferred_locales(request_locale=locale, preferred_locales=preferred_locales)
        ),
        "ts": time.time(),
    }


__all__ = [
    "ENTITY_ALIAS_ADDED",
    "ENTITY_ALIAS_CONFLICT_DETECTED",
    "ENTITY_ALIAS_DEPRECATED",
    "ENTITY_ALIAS_REMOVED",
    "ENTITY_DISPLAY_NAME_CHANGED",
    "ENTITY_DRAFT_NAME_SUGGESTED",
    "ENTITY_EVENT_TOPICS",
    "ENTITY_OBSERVED",
    "ENTITY_REGISTRY_CHANGED",
    "NAMED_ENTITY_REGISTRY_SCHEMA",
    "NAMED_ENTITY_REGISTRY_YJS_PATH",
    "ENTITY_RESOLUTION_AMBIGUOUS",
    "ENTITY_RESOLUTION_FAILED",
    "EntityAliasAction",
    "EntityAliasApplyStatus",
    "EntityAliasApplyResult",
    "EntityAliasProposal",
    "EntityLabel",
    "EntityResolutionAmbiguity",
    "EntityResolutionMatch",
    "EntityResolutionResult",
    "NamedEntityRecord",
    "NamedEntityService",
    "apply_alias_deprecate",
    "apply_alias_add",
    "apply_alias_remove",
    "canonical_device_ref",
    "compatibility_device_ref",
    "compact_registry_payload",
    "device_entity_lifecycle_event_envelopes",
    "entity_event_payload",
    "get_named_entity_service",
    "named_entity_registry_access_metadata",
    "list_entities",
    "normalize_entity_label",
    "propose_alias_deprecate",
    "propose_alias_add",
    "propose_alias_remove",
    "resolve_display_label",
    "resolve_text",
]
