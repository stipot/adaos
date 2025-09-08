# src/adaos/adapters/security/guard.py
from functools import wraps


def require_caps(required: set[str], subject_kw: str = "subject"):
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            capsvc = kw["capsvc"]
            subject = kw.get(subject_kw) or "unknown"
            if not capsvc.check(subject, required):
                raise PermissionError(f"missing caps: {required}")
            return fn(*a, **kw)

        return inner

    return deco
