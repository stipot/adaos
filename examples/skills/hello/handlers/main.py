from __future__ import annotations

from adaos.sdk.i18n import _
from adaos.sdk.events import publish
from adaos.sdk.memory import get, put
from adaos.sdk.validation.skill import validate_self


def on_start():
    count = get("runs", 0)
    put("runs", count + 1)
    publish("hello.started", {"count": count + 1})
    return _("prep.hello", count=count + 1)
