"""Core building blocks for the AdaOS SDK facades."""

from __future__ import annotations

from ._cap import require_cap
from ._ctx import require_ctx
from .errors import CapabilityError, ConflictError, QuotaExceeded, SdkError, SdkRuntimeNotInitialized
from .types import (
    Handler,
    Payload,
    ResultEnvelope,
    Topic,
    ToolFn,
    result_envelope,
    SCHEMA_REQUEST_BASE,
    SCHEMA_RESULT_ENVELOPE,
    SCHEMA_ISSUE,
    SCHEMA_VALIDATION_REPORT,
    SCHEMA_SKILL_META,
    SCHEMA_SCENARIO_META,
)

__all__ = [
    "require_cap",
    "require_ctx",
    "CapabilityError",
    "ConflictError",
    "QuotaExceeded",
    "SdkError",
    "SdkRuntimeNotInitialized",
    "Handler",
    "Payload",
    "ResultEnvelope",
    "Topic",
    "ToolFn",
    "result_envelope",
    "SCHEMA_REQUEST_BASE",
    "SCHEMA_RESULT_ENVELOPE",
    "SCHEMA_ISSUE",
    "SCHEMA_VALIDATION_REPORT",
    "SCHEMA_SKILL_META",
    "SCHEMA_SCENARIO_META",
]
