from __future__ import annotations

from dataclasses import dataclass

from adaos.services.agent_context import AgentContext

from ..skill.state import _validate_key as _validate_binding_key  # reuse validation rules


@dataclass(slots=True)
class ScenarioControlService:
    ctx: AgentContext

    def set_enabled(self, scenario_id: str, enabled: bool) -> None:
        key = f"scenarios/{scenario_id}/enabled"
        self.ctx.kv.set(key, bool(enabled))

    def set_binding(self, scenario_id: str, key: str, value: str) -> bool:
        repo = getattr(self.ctx, "scenarios_repo", None)
        if repo is not None:
            try:
                repo.ensure()
            except Exception:
                pass
            if repo.get(scenario_id) is None:
                return False
        storage_key = f"scenarios/{scenario_id}/bindings/{_validate_binding_key(key)}"
        self.ctx.kv.set(storage_key, value)
        return True
