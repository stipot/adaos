from __future__ import annotations

import pytest

from adaos.sdk.errors import CapabilityError
from adaos.sdk.manage import resources_request, resources_status
from adaos.services.agent_context import get_ctx


class AllowCaps:
    def __init__(self, allowed: set[str]):
        self._allowed = allowed

    def allows(self, capability: str) -> bool:
        return capability in self._allowed


def test_resources_request_and_status():
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.resources.request", "manage.resources.status"})

    res = resources_request("microphone", scope="read", request_id="req-r1")
    assert res["status"] == "pending"
    assert res["dry_run"] is False

    status = resources_status("req-r1")
    assert status["status"] == "pending"
    assert status["ticket"]["ticket_id"] == "req-r1"

    # repeated request id returns cached result
    assert resources_request("microphone", scope="read", request_id="req-r1") == res


def test_resources_request_dry_run():
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.resources.request"})

    preview = resources_request("camera", scope="write", request_id="req-preview", dry_run=True)
    assert preview["dry_run"] is True
    assert ctx.kv.get("resources/requests/req-preview") is None


def test_resources_status_not_found():
    ctx = get_ctx()
    ctx.caps = AllowCaps({"manage.resources.status"})
    assert resources_status("missing") == {"status": "not_found", "ticket": None}


def test_resources_request_missing_capability():
    ctx = get_ctx()
    ctx.caps = AllowCaps(set())
    with pytest.raises(CapabilityError):
        resources_request("microphone", scope="read", request_id="req-no-cap")
