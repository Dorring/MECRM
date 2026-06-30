"""
Compliance Agent

Responsible for:
- Data validation
- Policy enforcement
- Audit checks
- Risk assessment
"""

from typing import Dict, Any

import structlog

from .base import BaseAgent

logger = structlog.get_logger()


class ComplianceAgent(BaseAgent):
    """AI agent for compliance and policy enforcement."""
    
    def __init__(self):
        super().__init__(
            agent_id="compliance-agent",
            agent_type="compliance",
            capabilities=[
                "audit:validate",
                "policy:check",
                "risk:assess",
            ],
        )
        
    async def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Process a generic event."""
        return await self.validate_data(event)
        
    async def validate_data(
        self,
        event: Dict[str, Any],
        entity_type: str = "unknown",
    ) -> Dict[str, Any]:
        """Validate data for compliance issues."""
        tenant_id = event.get("tenantid", "")
        data = event.get("data", {})
        
        logger.info("Validating data", entity_type=entity_type, tenant_id=tenant_id)
        
        # Check for common compliance issues
        issues = []
        risk_score = 0
        
        # PII detection
        pii_fields = self._detect_pii(data)
        if pii_fields:
            issues.append({
                "type": "pii_detected",
                "severity": "medium",
                "fields": pii_fields,
                "recommendation": "Ensure PII is handled according to data protection policies",
            })
            risk_score += 20
            
        # Validate required fields
        missing_fields = self._check_required_fields(entity_type, data)
        if missing_fields:
            issues.append({
                "type": "missing_required_fields",
                "severity": "low",
                "fields": missing_fields,
                "recommendation": "Complete missing required fields",
            })
            risk_score += 10
            
        # Check for suspicious patterns
        suspicious = self._detect_suspicious_patterns(data)
        if suspicious:
            issues.append({
                "type": "suspicious_pattern",
                "severity": "high",
                "details": suspicious,
                "recommendation": "Review data for potential fraud or abuse",
            })
            risk_score += 40
            
        confidence = 0.9 if not issues else 0.7
        
        # Emit validation result
        await self.emit_event(
            topic="crm.agents.action-executed",
            event_type="crm.agents.data-validated",
            tenant_id=tenant_id,
            data={
                "entityType": entity_type,
                "entityId": data.get("leadId") or data.get("dealId") or data.get("ticketId"),
                "issues": issues,
                "riskScore": risk_score,
                "compliant": len(issues) == 0,
                "confidence": confidence,
                "validatedBy": self.agent_id,
            },
            correlation_id=event.get("correlationid"),
        )
        
        # If high risk, emit security event
        if risk_score >= 50:
            await self.emit_event(
                topic="crm.security.events",
                event_type="crm.security.risk-detected",
                tenant_id=tenant_id,
                data={
                    "eventType": "high_risk_data",
                    "severity": "high" if risk_score >= 70 else "medium",
                    "source": f"compliance-agent:{entity_type}",
                    "details": issues,
                    "riskScore": risk_score,
                },
            )
            
        logger.info(
            "Data validation completed",
            entity_type=entity_type,
            issues_count=len(issues),
            risk_score=risk_score,
        )
        
        return {
            "status": "completed",
            "compliant": len(issues) == 0,
            "issues": issues,
            "risk_score": risk_score,
        }
        
    def _detect_pii(self, data: Dict[str, Any]) -> list:
        """Detect potential PII fields."""
        pii_keywords = ["ssn", "social_security", "passport", "credit_card", "bank_account"]
        pii_fields = []
        
        for key, value in data.items():
            key_lower = key.lower()
            if any(kw in key_lower for kw in pii_keywords):
                pii_fields.append(key)
            elif isinstance(value, str):
                # Simple pattern detection
                if self._looks_like_ssn(value):
                    pii_fields.append(key)
                elif self._looks_like_credit_card(value):
                    pii_fields.append(key)
                    
        return pii_fields
        
    def _looks_like_ssn(self, value: str) -> bool:
        """Check if value looks like an SSN."""
        import re
        pattern = r'^\d{3}-?\d{2}-?\d{4}$'
        return bool(re.match(pattern, value.strip()))
        
    def _looks_like_credit_card(self, value: str) -> bool:
        """Check if value looks like a credit card number."""
        import re
        cleaned = re.sub(r'[\s-]', '', value)
        return bool(re.match(r'^\d{13,19}$', cleaned))
        
    def _check_required_fields(self, entity_type: str, data: Dict[str, Any]) -> list:
        """Check for missing required fields."""
        required_fields = {
            "lead": ["name"],
            "deal": ["name"],
            "ticket": ["subject"],
            "customer": ["name"],
        }
        
        required = required_fields.get(entity_type, [])
        return [f for f in required if not data.get(f)]
        
    def _detect_suspicious_patterns(self, data: Dict[str, Any]) -> list:
        """Detect suspicious patterns in data."""
        suspicious = []
        
        # Check for test/fake data patterns
        name = str(data.get("name", "")).lower()
        email = str(data.get("email", "")).lower()
        
        test_patterns = ["test", "fake", "sample", "demo", "xxx", "asdf"]
        if any(p in name for p in test_patterns):
            suspicious.append("Name contains test/fake pattern")
            
        # Check for disposable email domains
        disposable_domains = ["tempmail.com", "throwaway.com", "fakeinbox.com"]
        if any(d in email for d in disposable_domains):
            suspicious.append("Disposable email domain detected")
            
        return suspicious
