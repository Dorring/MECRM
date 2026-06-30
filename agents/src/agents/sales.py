"""
Sales Agent

Responsible for:
- Lead qualification and scoring
- Deal analysis and insights
- Next-best-action recommendations
"""

import json
import uuid
from typing import Dict, Any

import structlog

from .base import BaseAgent
from orchestrator.config import settings
from governance.approval_service import PendingAction

logger = structlog.get_logger()


class SalesAgent(BaseAgent):
    """AI agent for sales-related tasks."""
    
    def __init__(self):
        super().__init__(
            agent_id="sales-agent",
            agent_type="sales",
            capabilities=[
                "leads:qualify",
                "leads:score",
                "deals:analyze",
                "deals:recommend",
            ],
        )
        
    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Process a generic event."""
        event_type = event.get("type", "")
        
        if "leads" in event_type:
            return await self.qualify_lead(event)
        elif "deals" in event_type:
            return await self.analyze_deal(event)
            
        return {"status": "skipped", "reason": "Unknown event type"}
        
    async def qualify_lead(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Qualify a lead and assign a score."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        lead_id = data.get("leadId")
        
        logger.info("Qualifying lead", lead_id=lead_id, tenant_id=tenant_id)

        policy_precheck = await self.check_policy(
            tenant_id=tenant_id,
            action="leads:qualify",
            resource={"lead_id": lead_id},
            confidence=1.0,
        )
        if not policy_precheck["allowed"]:
            logger.warning("Policy denied lead qualification", reasons=policy_precheck["deny_reasons"])
            return {"status": "denied", "reasons": policy_precheck["deny_reasons"]}
        
        # Build prompt for lead qualification
        prompt = f"""Analyze this lead and provide a qualification score from 0-100.

Lead Information:
- Name: {data.get('name', 'Unknown')}
- Email: {data.get('email', 'Not provided')}
- Company: {data.get('company', 'Not provided')}
- Source: {data.get('source', 'Unknown')}

Provide your response in JSON format:
{{
    "score": <0-100>,
    "qualification_status": "qualified" | "unqualified" | "needs_info",
    "reasoning": "<brief explanation>",
    "confidence": <0.0-1.0>,
    "factors": [
        {{"name": "<factor>", "impact": "positive" | "negative", "weight": <0.0-1.0>}}
    ],
    "recommended_actions": ["<action1>", "<action2>"]
}}
"""

        system_prompt = """You are a sales qualification expert. Analyze leads based on:
1. Email domain quality (corporate vs free email)
2. Company information completeness
3. Source quality
4. Engagement signals

Be conservative with scores. High scores (80+) require strong signals.
Always explain your reasoning clearly."""

        try:
            # Call LLM
            response = await self.call_llm(prompt, system_prompt, tenant_id=tenant_id)
            
            # Parse response
            result = self._parse_json_response(response)
            
            score = result.get("score", 50)
            confidence = result.get("confidence", 0.7)
            reasoning = result.get("reasoning", "Analysis completed")
            
            # Check policy before taking action
            policy_result = await self.check_policy(
                tenant_id=tenant_id,
                action="leads:qualify",
                resource={"lead_id": lead_id, "score": score},
                confidence=confidence,
            )
            
            if not policy_result["allowed"]:
                logger.warning("Policy denied lead qualification", reasons=policy_result["deny_reasons"])
                return {"status": "denied", "reasons": policy_result["deny_reasons"]}
                
            # Emit reasoning for transparency
            await self.emit_reasoning(
                tenant_id=tenant_id,
                task_id=str(uuid.uuid4()),
                reasoning=reasoning,
                confidence=confidence,
                factors=result.get("factors", []),
            )
            
            # If high-impact action or low confidence, request approval
            if policy_result.get("requires_approval") or confidence < settings.DEFAULT_CONFIDENCE_THRESHOLD:
                approval_id = str(uuid.uuid4())
                if self._approval_service:
                    await self._approval_service.request_approval(
                        PendingAction(
                            tenant_id=tenant_id,
                            agent_id=self.agent_id,
                            approval_id=approval_id,
                            action_type="leads:qualify",
                            topic="crm.leads.qualified",
                            event_type="crm.leads.qualified",
                            data={
                                "leadId": lead_id,
                                "score": score,
                                "qualificationStatus": result.get("qualification_status"),
                                "reasoning": reasoning,
                                "confidence": confidence,
                                "recommendedActions": result.get("recommended_actions", []),
                                "qualifiedBy": self.agent_id,
                                "approvalId": approval_id,
                            },
                            correlation_id=event.get("correlationid"),
                        )
                    )

                await self.request_approval(
                    tenant_id=tenant_id,
                    action_type="leads:qualify",
                    target_entity="lead",
                    target_id=lead_id,
                    context={
                        "score": score,
                        "qualification_status": result.get("qualification_status"),
                        "recommended_actions": result.get("recommended_actions", []),
                    },
                    reasoning=reasoning,
                    confidence=confidence,
                    approval_id=approval_id,
                )
                return {"status": "pending_approval", "score": score}
                
            # Emit qualification result
            await self.emit_event(
                topic="crm.leads.qualified",
                event_type="crm.leads.qualified",
                tenant_id=tenant_id,
                data={
                    "leadId": lead_id,
                    "score": score,
                    "qualificationStatus": result.get("qualification_status"),
                    "reasoning": reasoning,
                    "confidence": confidence,
                    "recommendedActions": result.get("recommended_actions", []),
                    "qualifiedBy": self.agent_id,
                },
                correlation_id=event.get("correlationid"),
            )
            
            logger.info(
                "Lead qualified",
                lead_id=lead_id,
                score=score,
                status=result.get("qualification_status"),
            )
            
            return {
                "status": "completed",
                "score": score,
                "qualification_status": result.get("qualification_status"),
            }
            
        except Exception as e:
            logger.error("Lead qualification failed", lead_id=lead_id, error=str(e))
            return {"status": "failed", "error": str(e)}
            
    async def analyze_deal(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze a deal and provide insights."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        deal_id = data.get("dealId")
        
        logger.info("Analyzing deal", deal_id=deal_id, tenant_id=tenant_id)
        
        prompt = f"""Analyze this deal and provide insights.

Deal Information:
- Name: {data.get('name', 'Unknown')}
- Amount: ${data.get('amount', 0):,.2f}
- Stage: {data.get('stage', 'Unknown')}

Provide your response in JSON format:
{{
    "win_probability": <0-100>,
    "risk_factors": ["<risk1>", "<risk2>"],
    "opportunities": ["<opportunity1>"],
    "recommended_actions": ["<action1>"],
    "reasoning": "<brief explanation>",
    "confidence": <0.0-1.0>
}}
"""

        try:
            response = await self.call_llm(prompt, tenant_id=tenant_id)
            result = self._parse_json_response(response)
            
            await self.emit_event(
                topic="crm.agents.action-executed",
                event_type="crm.agents.deal-analyzed",
                tenant_id=tenant_id,
                data={
                    "dealId": deal_id,
                    "analysis": result,
                    "analyzedBy": self.agent_id,
                },
            )
            
            return {"status": "completed", "analysis": result}
            
        except Exception as e:
            logger.error("Deal analysis failed", deal_id=deal_id, error=str(e))
            return {"status": "failed", "error": str(e)}
            
    async def recommend_next_action(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Recommend next best action for a deal."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        deal_id = data.get("dealId")
        new_stage = data.get("newStage")
        
        logger.info("Recommending next action", deal_id=deal_id, stage=new_stage)
        
        prompt = f"""A deal has moved to the {new_stage} stage.

Previous stage: {data.get('previousStage', 'Unknown')}
New stage: {new_stage}
Deal amount: ${data.get('amount', 0)}

What should be the next best actions? Provide 3 specific, actionable recommendations.

Response format:
{{
    "recommendations": [
        {{"action": "<specific action>", "priority": "high|medium|low", "reasoning": "<why>"}}
    ],
    "confidence": <0.0-1.0>
}}
"""

        try:
            response = await self.call_llm(prompt, tenant_id=tenant_id)
            result = self._parse_json_response(response)
            
            await self.emit_event(
                topic="crm.agents.action-proposed",
                event_type="crm.agents.next-action-recommended",
                tenant_id=tenant_id,
                data={
                    "dealId": deal_id,
                    "stage": new_stage,
                    "recommendations": result.get("recommendations", []),
                    "recommendedBy": self.agent_id,
                },
            )
            
            return {"status": "completed", "recommendations": result.get("recommendations", [])}
            
        except Exception as e:
            logger.error("Recommendation failed", deal_id=deal_id, error=str(e))
            return {"status": "failed", "error": str(e)}
            
    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Parse JSON from LLM response."""
        try:
            # Try to extract JSON from response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except json.JSONDecodeError:
            pass
            
        # Return default structure if parsing fails
        return {
            "score": 50,
            "confidence": 0.5,
            "reasoning": response[:500] if response else "Unable to analyze",
            "factors": [],
        }
