from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from adaos.ports.root import *
from .keyring import ensure_ed25519, sign_nonce_ed25519, save_cert_bundle


@dataclass
class RootAccessService:
    hub_id: HubId
    team_id: TeamId
    ca_client: RootCAClientPort
    llm_client: RootLlmPort

    cert: Optional[CertBundle] = None
    pub: Optional[PubKey] = None

    def bootstrap_registration(self, *, email: str | None = None, phone: str | None = None) -> CertBundle:
        keyref, _ks = ensure_ed25519(self.hub_id)
        self.pub = keyref.pub
        begin = self.ca_client.begin_registration(BeginRegistrationReq(hub_id=self.hub_id, team_id=self.team_id, pubkey=keyref.pub, contact_email=email, contact_phone=phone))
        signature = sign_nonce_ed25519(self.hub_id, begin.nonce)
        self.cert = self.ca_client.complete_registration(CompleteRegistrationReq(reg_id=begin.reg_id, signature_b64=signature, want_alg="mtls"))
        # сохраним локально (под get_ctx().paths.base()/root/certs/)
        save_cert_bundle(self.hub_id, self.cert.cert_pem, self.cert.ca_pem)
        return self.cert

    def llm_chat(self, messages: list[dict], *, model_hint: str | None = None, tool_allowlist: list[str] | None = None, max_tokens: int | None = None) -> LlmProxyRes:
        if not self.cert:
            raise RuntimeError("not registered with root (cert missing)")
        return self.llm_client.llm_proxy(
            LlmProxyReq(session_id=None, model_hint=model_hint, messages=messages, tool_allowlist=tool_allowlist, max_tokens=max_tokens), mtls_cert=self.cert
        )
