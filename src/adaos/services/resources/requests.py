from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from adaos.services.agent_context import AgentContext


@dataclass(slots=True)
class ResourceTicket:
    ticket_id: str
    status: str
    payload: dict


@dataclass(slots=True)
class ResourceRequestService:
    ctx: AgentContext

    def create_ticket(self, ticket_id: str, payload: dict) -> ResourceTicket:
        key = f"resources/requests/{ticket_id}"
        existing = self.ctx.kv.get(key)
        if isinstance(existing, dict):
            return ResourceTicket(ticket_id=ticket_id, status=str(existing.get("status", "pending")), payload=existing)
        payload = {**payload, "status": payload.get("status", "pending"), "ticket_id": ticket_id}
        self.ctx.kv.set(key, payload)
        return ResourceTicket(ticket_id=ticket_id, status=str(payload.get("status", "pending")), payload=payload)

    def get_ticket(self, ticket_id: str) -> Optional[ResourceTicket]:
        key = f"resources/requests/{ticket_id}"
        value = self.ctx.kv.get(key)
        if not isinstance(value, dict):
            return None
        status = str(value.get("status", "pending"))
        return ResourceTicket(ticket_id=ticket_id, status=status, payload=value)
