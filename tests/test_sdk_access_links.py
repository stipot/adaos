from __future__ import annotations

from adaos.sdk.data import access_links as sdk_access_links


def test_browser_link_list_uses_metadata_draft_name_when_display_name_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        sdk_access_links._service,
        "browser_snapshot",
        lambda: [
            {
                "id": "dev_68e58ce1-2e6b-4615-9b0d-0e8cb46eccbb",
                "display_name": "",
                "access_class": "device",
                "browser_family": "Chrome",
                "os_name": "Windows",
                "form_factor": "Desktop",
                "last_seen_at": 1715000000.0,
            }
        ],
    )

    items = sdk_access_links.list_browser_links()

    assert items[0]["display_name"] == "Chrome on Windows"
    assert items[0]["effective_name"] == "Chrome on Windows"
    assert items[0]["draft_name"] == "Chrome on Windows"
    assert items[0]["display_name_source"] == "browser_metadata"


def test_get_browser_link_preserves_user_display_name_over_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        sdk_access_links._service,
        "get_link",
        lambda kind, device_id: {
            "id": device_id,
            "kind": kind,
            "display_name": "Dev Browser",
            "browser_family": "Chrome",
            "os_name": "Windows",
        },
    )

    item = sdk_access_links.get_browser_link("browser-1")

    assert item is not None
    assert item["display_name"] == "Dev Browser"
    assert item["effective_name"] == "Dev Browser"
    assert item["display_name_source"] == "policy"
