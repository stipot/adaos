from __future__ import annotations

from typing import Any, Mapping

from adaos.services import access_links as _service


_TITLE_TOKENS = {
    "chrome": "Chrome",
    "chromium": "Chromium",
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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _title_token(value: Any) -> str:
    token = _text(value)
    if not token:
        return ""
    folded = token.casefold().replace("_", " ").replace("-", " ")
    return _TITLE_TOKENS.get(folded, " ".join(part.capitalize() for part in folded.split()))


def _browser_draft_name(entry: Mapping[str, Any]) -> str:
    browser = _title_token(
        entry.get("browser_family")
        or entry.get("browser_name")
        or entry.get("browser")
    )
    os_name = _title_token(entry.get("os_name") or entry.get("os") or entry.get("platform"))
    form_factor = _title_token(entry.get("form_factor") or entry.get("device_type"))
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
    return ""


def _enrich_browser_link(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    out = dict(entry)
    display_name = _text(out.get("display_name"))
    hostname = _text(out.get("hostname"))
    draft_name = _browser_draft_name(out)
    entry_id = _text(out.get("id"))
    effective_name = display_name or hostname or draft_name or entry_id or "browser"

    out["effective_name"] = effective_name
    if not _text(out.get("title")):
        out["title"] = effective_name
    if draft_name:
        out["draft_name"] = draft_name
        out["suggested_display_name"] = draft_name
    if not display_name and (hostname or draft_name):
        out["display_name"] = hostname or draft_name
        out["display_name_source"] = "hostname" if hostname else "browser_metadata"
    elif display_name:
        out["display_name_source"] = "policy"
    return out


def list_browser_links() -> list[dict[str, Any]]:
    return [
        item
        for item in (_enrich_browser_link(entry) for entry in _service.browser_snapshot())
        if item is not None
    ]


def list_member_links() -> list[dict[str, Any]]:
    return _service.member_snapshot()


def get_browser_link(device_id: str) -> dict[str, Any] | None:
    return _enrich_browser_link(_service.get_link("browser", device_id))


def get_member_link(node_id: str) -> dict[str, Any] | None:
    return _service.get_link("member", node_id)


def rename_browser_link(device_id: str, display_name: str) -> dict[str, Any]:
    return _service.rename_link("browser", device_id, display_name)


def rename_member_link(node_id: str, display_name: str) -> dict[str, Any]:
    return _service.rename_link("member", node_id, display_name)


def add_browser_alias(
    device_id: str,
    alias: str,
    *,
    locale: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _service.add_link_alias(
        "browser",
        device_id,
        alias,
        locale=locale,
        actor=actor,
        source="sdk.data.access_links",
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    )


def remove_browser_alias(
    device_id: str,
    alias: str,
    *,
    locale: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _service.remove_link_alias(
        "browser",
        device_id,
        alias,
        locale=locale,
        actor=actor,
        source="sdk.data.access_links",
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    )


def deprecate_browser_alias(
    device_id: str,
    alias: str,
    *,
    locale: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _service.deprecate_link_alias(
        "browser",
        device_id,
        alias,
        locale=locale,
        actor=actor,
        source="sdk.data.access_links",
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    )


def add_member_alias(
    node_id: str,
    alias: str,
    *,
    locale: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _service.add_link_alias(
        "member",
        node_id,
        alias,
        locale=locale,
        actor=actor,
        source="sdk.data.access_links",
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    )


def remove_member_alias(
    node_id: str,
    alias: str,
    *,
    locale: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _service.remove_link_alias(
        "member",
        node_id,
        alias,
        locale=locale,
        actor=actor,
        source="sdk.data.access_links",
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    )


def deprecate_member_alias(
    node_id: str,
    alias: str,
    *,
    locale: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
    base_fingerprint: str | None = None,
) -> dict[str, Any]:
    return _service.deprecate_link_alias(
        "member",
        node_id,
        alias,
        locale=locale,
        actor=actor,
        source="sdk.data.access_links",
        request_id=request_id,
        base_fingerprint=base_fingerprint,
    )


def set_browser_lifetime(device_id: str, preset: str) -> dict[str, Any]:
    return _service.set_link_lifetime("browser", device_id, preset)


def set_member_lifetime(node_id: str, preset: str) -> dict[str, Any]:
    return _service.set_link_lifetime("member", node_id, preset)


def detach_browser_link(device_id: str) -> dict[str, Any]:
    return _service.detach_link("browser", device_id)


def detach_member_link(node_id: str) -> dict[str, Any]:
    return _service.detach_link("member", node_id)


def lifetime_label(entry: dict[str, Any]) -> str:
    return _service.lifetime_label(entry)
