"""
Base Agent class with common functionality.
"""

import json
import time
import uuid
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime, timezone

import structlog
import httpx
from aiokafka import AIOKafkaProducer
from langchain_core.messages import HumanMessage, SystemMessage

from intelligence.providers import create_chat_model
from orchestrator.config import settings
from governance.guard import GovernanceGuard
from governance.approval_service import approval_requestor_uuid
from governance.approval_service import ApprovalService
from governance.data_guard import DataGuard
from governance.explainability import DecisionArtifact, ExplainabilityEngine
from governance.agent_telemetry import inc_approval_required, inc_error, inc_policy_violation, inc_tool_call, observe_decision_latency

logger = structlog.get_logger()


class BaseAgent(ABC):
    """Base class for all AI agents."""
    
    def __init__(self, agent_id: str, agent_type: str, capabilities: list):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.capabilities = capabilities
        self.producer: Optional[AIOKafkaProducer] = None
        self.http_client: Optional[httpx.AsyncClient] = None
        self._llm: Any | None = None
        self._governance_guard: GovernanceGuard | None = None
        self._data_guard: DataGuard | None = None
        self._approval_service: ApprovalService | None = None
        self._explainability: ExplainabilityEngine | None = None
        
    async def initialize(self, producer: AIOKafkaProducer):
        """Initialize the agent with dependencies."""
        self.producer = producer
        self.http_client = httpx.AsyncClient(timeout=30.0)
        logger.info(f"{self.agent_id} initialized")

    def set_governance_guard(self, guard: GovernanceGuard) -> None:
        self._governance_guard = guard

    def set_data_guard(self, guard: DataGuard) -> None:
        self._data_guard = guard

    def set_approval_service(self, approval_service: ApprovalService) -> None:
        self._approval_service = approval_service

    def set_explainability_engine(self, engine: ExplainabilityEngine) -> None:
        self._explainability = engine
        
    async def cleanup(self):
        """Cleanup resources."""
        if self.http_client:
            await self.http_client.aclose()
            
    async def check_policy(
        self,
        tenant_id: str,
        action: str,
        resource: Dict[str, Any],
        confidence: float,
    ) -> Dict[str, Any]:
        """Check OPA policy before taking action."""
        try:
            started = time.perf_counter()
            inc_tool_call(agent_id=self.agent_id, tool_name="opa")

            policy_input = {
                "agent": {"id": self.agent_id, "type": self.agent_id},
                "tenant_id": tenant_id,
                "action": action,
                "context": resource,
                "resource": resource,
                "confidence": confidence,
            }

            if not self.http_client:
                raise RuntimeError("http_client not initialized")

            core_resp = await self.http_client.post(
                f"{settings.OPA_URL}/v1/data/enterprise_crm/agents",
                json={"input": policy_input},
            )
            core = core_resp.json().get("result", {}) or {}

            approval_resp = await self.http_client.post(
                f"{settings.OPA_URL}/v1/data/enterprise_crm/agents/approval",
                json={"input": policy_input},
            )
            approval = approval_resp.json().get("result", {}) or {}

            allowed = bool(core.get("allow", False))
            requires_approval = bool(core.get("requires_approval", False)) or bool(approval.get("requires_approval", False))

            deny_reasons = []
            if not allowed:
                deny_reasons.append("capability_not_allowed")
                inc_policy_violation(agent_id=self.agent_id, action_type=action)

            observe_decision_latency(agent_id=self.agent_id, action_type=action, risk_level="policy", status="checked", duration_ms=(time.perf_counter() - started) * 1000.0)
            return {
                "allowed": allowed,
                "requires_approval": requires_approval,
                "deny_reasons": deny_reasons,
                "approvers": approval.get("approvers", []),
                "priority": approval.get("priority", "normal"),
            }
        except Exception as e:
            logger.error("Policy check failed", error=str(e))
            inc_error(agent_id=self.agent_id, error_type="opa")
            # Fail closed - deny if policy engine unavailable
            return {"allowed": False, "requires_approval": True, "deny_reasons": ["Policy engine unavailable"]}
            
    async def emit_event(
        self,
        topic: str,
        event_type: str,
        tenant_id: str,
        data: Dict[str, Any],
        correlation_id: Optional[str] = None,
    ):
        """Emit an event to Kafka."""
        if self._data_guard and tenant_id:
            customer_id, user_id = _extract_subjects(data)
            if customer_id or user_id:
                await self._data_guard.ensure_allowed(tenant_id=tenant_id, agent_id=self.agent_id, customer_id=customer_id, user_id=user_id)
        if self._governance_guard and tenant_id:
            await self._governance_guard.ensure_allowed(tenant_id=tenant_id, agent_id=self.agent_id)

        event = {
            "specversion": "1.0",
            "type": event_type,
            "source": f"/agents/{self.agent_id}",
            "id": str(uuid.uuid4()),
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "datacontenttype": "application/json",
            "tenantid": tenant_id,
            "correlationid": correlation_id or str(uuid.uuid4()),
            "data": data,
        }
        
        if not self.producer:
            raise RuntimeError("producer not initialized")

        await self.producer.send(
            topic,
            value=json.dumps(event),
            key=tenant_id.encode() if tenant_id else None,
        )
        
        logger.debug("Event emitted", topic=topic, event_type=event_type)

        if self._explainability and tenant_id:
            await self._explainability.record_decision(
                DecisionArtifact(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    agent_id=self.agent_id,
                    action_type=event_type,
                    risk_level=str(data.get("riskLevel") or "LOW"),
                    status="executed",
                    confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
                    input_context={},
                    reasoning={"factors": data.get("factors")},
                    evidence=[
                        {"type": "kafka_topic", "source_id": topic},
                        {"type": "event_id", "source_id": event["id"]},
                    ],
                    tool_calls=[],
                    approval_id=str(data.get("approvalId")) if data.get("approvalId") else None,
                    correlation_id=str(event.get("correlationid")) if event.get("correlationid") else None,
                )
            )
        
    async def emit_reasoning(
        self,
        tenant_id: str,
        task_id: str,
        reasoning: str,
        confidence: float,
        factors: list,
    ):
        """Emit reasoning for transparency."""
        if self._governance_guard and tenant_id:
            await self._governance_guard.ensure_allowed(tenant_id=tenant_id, agent_id=self.agent_id)

        await self.emit_event(
            topic="crm.agents.reasoning",
            event_type="crm.agents.reasoning",
            tenant_id=tenant_id,
            data={
                "agentId": self.agent_id,
                "taskId": task_id,
                "reasoning": reasoning,
                "confidence": confidence,
                "factors": factors,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        
    async def request_approval(
        self,
        tenant_id: str,
        action_type: str,
        target_entity: str,
        target_id: str,
        context: Dict[str, Any],
        reasoning: str,
        confidence: float,
        approval_id: Optional[str] = None,
    ):
        """Request human approval for an action."""
        if self._data_guard and tenant_id:
            await self._data_guard.ensure_allowed(tenant_id=tenant_id, agent_id=self.agent_id)
        if self._governance_guard and tenant_id:
            await self._governance_guard.ensure_allowed(tenant_id=tenant_id, agent_id=self.agent_id)

        inc_approval_required(agent_id=self.agent_id, action_type=action_type)

        await self.emit_event(
            topic="crm.approvals.required",
            event_type="crm.approvals.required",
            tenant_id=tenant_id,
            data={
                "approvalId": approval_id,
                "requestorType": "agent",
                "requestorId": approval_requestor_uuid(self.agent_id),
                "actionType": action_type,
                "targetEntity": target_entity,
                "targetId": target_id,
                "context": context,
                "reasoning": reasoning,
                "confidence": confidence,
                "agentType": self.agent_type,
            },
        )
        
        logger.info(
            "Approval requested",
            agent=self.agent_id,
            action=action_type,
            target=f"{target_entity}:{target_id}",
        )
        
    async def call_llm(self, prompt: str, system_prompt: Optional[str] = None, tenant_id: Optional[str] = None) -> str:
        """Call the LLM for inference."""
        try:
            if self._governance_guard and tenant_id:
                await self._governance_guard.ensure_allowed(tenant_id=tenant_id, agent_id=self.agent_id)

            started = time.perf_counter()
            inc_tool_call(agent_id=self.agent_id, tool_name="llm")

            messages: list[Any] = []
            if system_prompt:
                messages.append(SystemMessage(content=system_prompt))
            messages.append(HumanMessage(content=prompt))

            if self._llm is None:
                self._llm = create_chat_model(temperature=0)
            response = await self._llm.ainvoke(messages)
            content = getattr(response, "content", response)
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            result = str(content or "")
            observe_decision_latency(agent_id=self.agent_id, action_type="llm", risk_level="tool", status="ok", duration_ms=(time.perf_counter() - started) * 1000.0)
            return result
            
        except Exception as e:
            logger.error("LLM call failed", error=str(e))
            inc_error(agent_id=self.agent_id, error_type="llm")
            raise
            
    @abstractmethod
    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Process an event - must be implemented by subclasses."""
        pass


def _extract_subjects(data: Dict[str, Any]) -> tuple[str | None, str | None]:
    customer_id = data.get("customerId") or data.get("customer_id")
    user_id = data.get("userId") or data.get("user_id") or data.get("createdBy") or data.get("assignedTo")
    return (str(customer_id) if customer_id else None, str(user_id) if user_id else None)
