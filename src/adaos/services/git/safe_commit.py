from __future__ import annotations
import re
from pathlib import Path
from typing import Iterable

_DENY = [
    r".*\.pem$",
    r".*\.key$",
    r".*\.pfx$",
    r".*\.p12$",
    r".*vault\.json$",
    r".*\.env$",
    r".*\.env\..*$",
    r".*secrets.*\.json$",
    r".*_secrets\.json$",
]
_DENY_RE = [re.compile(p, re.IGNORECASE) for p in _DENY]


def sanitize_message(msg: str) -> str:
    msg = msg.replace("\r", "")
    lines = msg.split("\n")
    head = lines[0][:72]
    tail = [ln.rstrip() for ln in lines[1:]]
    return "\n".join([head, *tail]).strip()


def check_no_denied(files: Iterable[str]) -> list[str]:
    bad = []
    for f in files:
        for rx in _DENY_RE:
            if rx.match(f):
                bad.append(f)
                break
    return bad
