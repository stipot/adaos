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

    def _ticket_keys(self, ticket_id: str) -> list[str]:
        keys: list[str] = []
        skill_ctx = getattr(self.ctx, "skill_ctx", None)
        if skill_ctx is not None:
            current = skill_ctx.get()
            skill_name = getattr(current, "name", None)
            if skill_name:
                keys.append(f"skills/{skill_name}/resources/requests/{ticket_id}")
        keys.append(f"resources/requests/{ticket_id}")
        return keys

    def create_ticket(self, ticket_id: str, payload: dict) -> ResourceTicket:
        keys = self._ticket_keys(ticket_id)
        for key in keys:
            existing = self.ctx.kv.get(key)
            if isinstance(existing, dict):
                return ResourceTicket(ticket_id=ticket_id, status=str(existing.get("status", "pending")), payload=existing)
        payload = {**payload, "status": payload.get("status", "pending"), "ticket_id": ticket_id}
        self.ctx.kv.set(keys[0], payload)
        return ResourceTicket(ticket_id=ticket_id, status=str(payload.get("status", "pending")), payload=payload)

    def get_ticket(self, ticket_id: str) -> Optional[ResourceTicket]:
        for key in self._ticket_keys(ticket_id):
            value = self.ctx.kv.get(key)
            if isinstance(value, dict):
                status = str(value.get("status", "pending"))
                return ResourceTicket(ticket_id=ticket_id, status=status, payload=value)
        return None
