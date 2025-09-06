from __future__ import annotations
from functools import wraps


def require_caps(caps, subject="core"):
    if isinstance(caps, str):
        caps = [caps]

    def deco(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            self.caps.require(subject, *caps)  # ожидаем, что у self есть .caps
            return fn(self, *args, **kwargs)

        return wrapper

    return deco
