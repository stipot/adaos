from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Literal, Optional, Sequence
from datetime import datetime

HubId = str
TeamId = str


@dataclass
class PubKey:
    alg: Literal["ed25519", "rsa-3072"]
    pem: str  # PEM of public key
    fingerprint: str  # sha256 hex


@dataclass
class KeypairRef:
    hub_id: HubId
    pub: PubKey
    created_at: datetime


@dataclass
class CertBundle:
    cert_pem: str  # client cert (PEM)
    ca_pem: str  # root CA (PEM chain)
    expires_at: datetime


@dataclass
class BeginRegistrationReq:
    hub_id: HubId
    team_id: TeamId
    pubkey: PubKey
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None


@dataclass
class BeginRegistrationRes:
    reg_id: str
    nonce: str  # base64url


@dataclass
class CompleteRegistrationReq:
    reg_id: str
    signature_b64: str
    want_alg: Literal["mtls", "jwt"] = "mtls"


@dataclass
class LlmProxyReq:
    session_id: Optional[str]
    model_hint: Optional[str]
    messages: list[dict]
    tool_allowlist: Optional[Sequence[str]] = None
    max_tokens: Optional[int] = None
    timeout_ms: Optional[int] = 30000


@dataclass
class LlmProxyRes:
    session_id: str
    output: dict
    finish_reason: Literal["stop", "length", "tool_call", "error"]


class RootCAClientPort(Protocol):
    def begin_registration(self, req: BeginRegistrationReq) -> BeginRegistrationRes: ...
    def complete_registration(self, req: CompleteRegistrationReq) -> CertBundle: ...


class RootLlmPort(Protocol):
    def llm_proxy(self, req: LlmProxyReq, *, mtls_cert: Optional[CertBundle]) -> LlmProxyRes: ...
