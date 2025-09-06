from __future__ import annotations
from collections import defaultdict
from typing import Set, Dict


class InMemoryCapabilities:
    def __init__(self) -> None:
        self._caps: Dict[str, Set[str]] = defaultdict(set)

    def grant(self, subject: str, *caps: str) -> None:
        self._caps[subject].update(caps)

    def revoke(self, subject: str, *caps: str) -> None:
        self._caps[subject].difference_update(caps)

    def has(self, subject: str, cap: str) -> bool:
        if cap in self._caps.get(subject, ()):
            return True
        # поддержка префиксов: "net.*"
        pref = cap.split(".", 1)[0] + ".*"
        return pref in self._caps.get(subject, ())

    def require(self, subject: str, *caps: str) -> None:
        missing = [c for c in caps if not self.has(subject, c)]
        if missing:
            raise PermissionError(f"capabilities missing for '{subject}': {', '.join(missing)}")
