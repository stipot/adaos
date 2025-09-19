from __future__ import annotations

import pytest

from adaos.sdk import events
from adaos.sdk.errors import SdkRuntimeNotInitialized
from adaos.services.agent_context import clear_ctx


def test_events_publish_no_ctx():
    clear_ctx()
    with pytest.raises(SdkRuntimeNotInitialized):
        events.publish("demo.event", {"foo": "bar"})
