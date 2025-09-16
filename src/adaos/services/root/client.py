from __future__ import annotations
import httpx, json, os
from dataclasses import asdict
from typing import Optional

from adaos.ports.root import (
    RootCAClientPort,
    RootLlmPort,
    BeginRegistrationReq,
    BeginRegistrationRes,
    CompleteRegistrationReq,
    CertBundle,
    LlmProxyReq,
    LlmProxyRes,
)

DEFAULT_ROOT_URL = os.getenv("ADAOS_ROOT_URL", "https://api.inimatic.com")


class RootHttpClient(RootCAClientPort, RootLlmPort):
    def __init__(self, base_url: Optional[str] = None):
        self.base = (base_url or DEFAULT_ROOT_URL).rstrip("/")

    def begin_registration(self, req: BeginRegistrationReq) -> BeginRegistrationRes:
        r = httpx.post(f"{self.base}/v1/root/register/begin", json=asdict(req), timeout=30.0)
        r.raise_for_status()
        return BeginRegistrationRes(**r.json())

    def complete_registration(self, req: CompleteRegistrationReq) -> CertBundle:
        r = httpx.post(f"{self.base}/v1/root/register/complete", json=asdict(req), timeout=30.0)
        r.raise_for_status()
        return CertBundle(**r.json())

    def llm_proxy(self, req: LlmProxyReq, *, mtls_cert: CertBundle | None) -> LlmProxyRes:
        # mTLS прикрутим на этапе 3 (когда root вернёт клиентский ключ)
        r = httpx.post(f"{self.base}/v1/root/llm/proxy", json=asdict(req), timeout=30.0)
        r.raise_for_status()
        return LlmProxyRes(**r.json())
