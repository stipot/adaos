from __future__ import annotations

from io import StringIO
from pathlib import Path
import sys

from adaos.services.scenario_runner_min import run_from_file
from adaos.sdk.data import memory

SCENARIO = Path('.adaos/scenarios/greet_on_boot/scenario.yaml')


def test_greet_with_name():
    memory.put('user.name', 'Ада')

    result = run_from_file(str(SCENARIO))
    msg = result.get('msg')

    assert isinstance(msg, str)
    assert 'Ада' in msg

    memory.delete('user.name')


def test_greet_without_name(monkeypatch):
    memory.delete('user.name')

    monkeypatch.setattr(sys, 'stdin', StringIO('ТестИмя\n'))
    result = run_from_file(str(SCENARIO))
    msg = result.get('msg')

    assert isinstance(msg, str)
    assert 'ТестИмя' in msg

    assert memory.get('user.name') == 'ТестИмя'
