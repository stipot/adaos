from __future__ import annotations

from adaos.sdk.data.i18n import _
from adaos.services.agent_context import clear_ctx


def test_i18n_preboot():
    clear_ctx()
    assert _("cli.help") == "AdaOS CLI – managing skills, tests and Runtime"
