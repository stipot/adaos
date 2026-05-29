from __future__ import annotations

import pytest

from adaos.domain import (
    modal_projection_key,
    node_scoped_projection_key,
    page_projection_key,
    panel_projection_key,
    status_card_id_from_projection_key,
    status_card_projection_key,
    surface_projection_key,
    widget_projection_key,
)
from adaos.services.status_projection import status_card_projection_key as registry_status_card_projection_key


def test_status_card_projection_keys_are_deterministic() -> None:
    assert status_card_projection_key("runtime") == "status-card:runtime"
    assert status_card_projection_key(" infrascope-overview ") == "status-card:infrascope-overview"
    assert status_card_id_from_projection_key("status-card:runtime") == "runtime"
    assert registry_status_card_projection_key("runtime") == status_card_projection_key("runtime")


def test_surface_projection_keys_cover_browser_surfaces() -> None:
    assert page_projection_key("infrascope") == "projection:page/infrascope"
    assert widget_projection_key("infra-state") == "projection:widget/infra-state"
    assert modal_projection_key("runtime-details") == "projection:modal/runtime-details"
    assert panel_projection_key("runtime") == "projection:panel/runtime"
    assert surface_projection_key("widget", "weather") == "projection:widget/weather"


def test_node_scoped_projection_keys_wrap_existing_projection_keys() -> None:
    assert (
        node_scoped_projection_key(node_id="node-a", projection_key="status-card:runtime")
        == "projection:node/node-a/status-card:runtime"
    )
    assert (
        widget_projection_key("infra-state", node_id="node/a")
        == "projection:node/node-a/projection:widget/infra-state"
    )


def test_projection_key_helpers_reject_ambiguous_inputs() -> None:
    with pytest.raises(ValueError, match="card_id is required"):
        status_card_projection_key("")
    with pytest.raises(ValueError, match="unsupported surface projection kind"):
        surface_projection_key("toast", "runtime")
    with pytest.raises(ValueError, match="status-card projection key is required"):
        status_card_id_from_projection_key("projection:widget/runtime")
