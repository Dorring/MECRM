from dataclasses import dataclass
from typing import Optional

from .kill_switch import AgentKillSwitch, KillSwitchDecision
from .agent_telemetry import inc_kill_switch_block


@dataclass(frozen=True)
class GovernanceBlock:
    reason: str
    kill_switch: Optional[KillSwitchDecision] = None


class GovernanceGuard:
    def __init__(self, kill_switch: AgentKillSwitch):
        self._kill_switch = kill_switch

    async def ensure_allowed(self, *, tenant_id: str, agent_id: str) -> None:
        decision = await self._kill_switch.decision(tenant_id=tenant_id, agent_id=agent_id)
        if decision.blocked:
            inc_kill_switch_block(scope=decision.scope_key)
            raise GovernanceBlocked(
                GovernanceBlock(
                    reason="blocked_by_kill_switch",
                    kill_switch=decision,
                )
            )


class GovernanceBlocked(Exception):
    def __init__(self, block: GovernanceBlock):
        super().__init__(block.reason)
        self.block = block
