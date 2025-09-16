from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
import os

from adaos.services.root.client import RootHttpClient
from adaos.services.root.service import RootAccessService

router = APIRouter(prefix="/v1/root", tags=["root"])

_root = RootAccessService(
    # TODO Взять из hub - node_id
    hub_id=os.getenv("ADAOS_HUB_ID", "hub-001"),
    # TODO Взять из subnet_id
    team_id=os.getenv("ADAOS_TEAM_ID", "team-default"),
    ca_client=RootHttpClient(),
    llm_client=RootHttpClient(),
)


class RegReq(BaseModel):
    email: str | None = None
    phone: str | None = None


@router.post("/register")
def root_register(req: RegReq):
    cert = _root.bootstrap_registration(email=req.email, phone=req.phone)
    return {"expires_at": cert.expires_at.isoformat() + "Z"}


class ChatReq(BaseModel):
    prompt: str
    model_hint: str | None = None


@router.post("/llm/chat")
def llm_chat(req: ChatReq):
    res = _root.llm_chat([{"role": "user", "content": req.prompt}], model_hint=req.model_hint, max_tokens=512)
    return {"session_id": res.session_id, "output": res.output}
