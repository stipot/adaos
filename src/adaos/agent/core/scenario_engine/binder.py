from __future__ import annotations
from typing import Dict, Any


def resolve_slots(prototype: Dict[str, Any], bindings: Dict[str, Any], registry: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Возвращает словарь {slotName: {type:'skill', id:'skill_name'}}.
    Берём из bindings.slots, иначе (mvp) — не резолвим автоматически.
    """
    out: Dict[str, Any] = {}
    slots = prototype.get("slots") or {}
    bslots = bindings.get("slots") or {}
    for name, slot in slots.items():
        b = bslots.get(name)
        if b and isinstance(b, dict) and "skill" in b:
            out[name] = {"type": "skill", "id": b["skill"]}
        elif b and isinstance(b, str):
            out[name] = {"type": "skill", "id": b}
        else:
            # если нужно — можно попытаться искать по registry[cap]
            pass
    return out
