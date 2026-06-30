"""
Twin Builder - Builds behavioral profiles from customer data.

Aggregates data from journey intelligence, analytics, conversations,
payments, and tickets to create a comprehensive behavioral model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import asyncpg
import structlog
from opentelemetry import trace

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TwinProfile:
    """Behavioral profile for a customer twin."""
    customer_id: str
    tenant_id: str
    
    # Behavioral features
    engagement_score: float = 0.0
    price_sensitivity: float = 0.5
    churn_sensitivity: float = 0.5
    conversion_likelihood: float = 0.5
    support_dependency: float = 0.0
    
    # Interaction patterns
    avg_response_time_hours: float | None = None
    ticket_frequency_30d: int = 0
    deal_velocity_days: float | None = None
    payment_reliability: float = 1.0
    
    # Feature vectors for predictions
    features: dict[str, Any] = field(default_factory=dict)
    
    # Metadata
    last_updated: str = ""
    confidence: float = 0.0


class TwinBuilder:
    """Builds customer twins from aggregated data."""
    
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn
    
    async def build_twin(self, *, tenant_id: str, customer_id: str) -> TwinProfile:
        """Build or refresh a customer twin profile."""
        with tracer.start_as_current_span("twin_builder.build_twin") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("customer_id", customer_id)
            
            # Extract features from various sources
            journey_features = await self._extract_journey_features(tenant_id, customer_id)
            ticket_features = await self._extract_ticket_features(tenant_id, customer_id)
            deal_features = await self._extract_deal_features(tenant_id, customer_id)
            payment_features = await self._extract_payment_features(tenant_id, customer_id)
            
            # Combine all features
            all_features = {
                **journey_features,
                **ticket_features,
                **deal_features,
                **payment_features,
            }
            
            # Calculate behavioral scores
            engagement_score = self._calculate_engagement_score(all_features)
            price_sensitivity = self._calculate_price_sensitivity(all_features)
            churn_sensitivity = self._calculate_churn_sensitivity(all_features)
            conversion_likelihood = self._calculate_conversion_likelihood(all_features)
            support_dependency = self._calculate_support_dependency(all_features)
            
            # Calculate overall confidence based on data availability
            confidence = self._calculate_confidence(all_features)
            
            profile = TwinProfile(
                customer_id=customer_id,
                tenant_id=tenant_id,
                engagement_score=engagement_score,
                price_sensitivity=price_sensitivity,
                churn_sensitivity=churn_sensitivity,
                conversion_likelihood=conversion_likelihood,
                support_dependency=support_dependency,
                avg_response_time_hours=all_features.get("avg_response_time_hours"),
                ticket_frequency_30d=int(all_features.get("tickets_30d", 0)),
                deal_velocity_days=all_features.get("deal_velocity_days"),
                payment_reliability=all_features.get("payment_reliability", 1.0),
                features=all_features,
                last_updated=_utc_now().isoformat(),
                confidence=confidence,
            )
            
            # Persist the twin profile
            await self._persist_twin(profile)
            
            logger.debug(
                "Twin profile built",
                tenant_id=tenant_id,
                customer_id=customer_id,
                confidence=confidence,
            )
            
            return profile
    
    async def get_twin(self, *, tenant_id: str, customer_id: str) -> TwinProfile | None:
        """Retrieve existing twin profile if fresh enough."""
        row = await self._conn.fetchrow(
            """
            SELECT customer_id::text, tenant_id::text, embedding_profile, behavior_features, last_updated
            FROM customer_twins
            WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
            """,
            tenant_id,
            customer_id,
        )
        if not row:
            return None
        
        # Check freshness (rebuild if older than 24 hours)
        last_updated = row["last_updated"]
        if last_updated:
            age = _utc_now() - last_updated.replace(tzinfo=timezone.utc)
            if age > timedelta(hours=24):
                return None
        
        features = dict(row["behavior_features"] or {})
        return TwinProfile(
            customer_id=str(row["customer_id"]),
            tenant_id=str(row["tenant_id"]),
            engagement_score=float(features.get("engagement_score", 0)),
            price_sensitivity=float(features.get("price_sensitivity", 0.5)),
            churn_sensitivity=float(features.get("churn_sensitivity", 0.5)),
            conversion_likelihood=float(features.get("conversion_likelihood", 0.5)),
            support_dependency=float(features.get("support_dependency", 0)),
            ticket_frequency_30d=int(features.get("tickets_30d", 0)),
            features=features,
            last_updated=last_updated.isoformat() if last_updated else "",
            confidence=float(features.get("confidence", 0)),
        )
    
    async def _persist_twin(self, profile: TwinProfile) -> None:
        """Store twin profile in database."""
        behavior_features = {
            "engagement_score": profile.engagement_score,
            "price_sensitivity": profile.price_sensitivity,
            "churn_sensitivity": profile.churn_sensitivity,
            "conversion_likelihood": profile.conversion_likelihood,
            "support_dependency": profile.support_dependency,
            "tickets_30d": profile.ticket_frequency_30d,
            "confidence": profile.confidence,
            **profile.features,
        }
        
        await self._conn.execute(
            """
            INSERT INTO customer_twins (tenant_id, customer_id, behavior_features, last_updated)
            VALUES ($1::uuid, $2::uuid, $3::jsonb, now())
            ON CONFLICT (tenant_id, customer_id)
            DO UPDATE SET behavior_features = $3::jsonb, last_updated = now()
            """,
            profile.tenant_id,
            profile.customer_id,
            behavior_features,
        )
    
    async def _extract_journey_features(self, tenant_id: str, customer_id: str) -> dict[str, Any]:
        """Extract features from customer journey."""
        row = await self._conn.fetchrow(
            """
            SELECT stage, confidence, features
            FROM customer_timeline_view
            WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
            """,
            tenant_id,
            customer_id,
        )
        if not row:
            return {"journey_stage": None}
        
        return {
            "journey_stage": row.get("stage"),
            "journey_confidence": float(row.get("confidence") or 0),
        }
    
    async def _extract_ticket_features(self, tenant_id: str, customer_id: str) -> dict[str, Any]:
        """Extract features from ticket history."""
        since = _utc_now() - timedelta(days=30)
        row = await self._conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE created_at >= $3)::int as tickets_30d,
              COUNT(*) FILTER (WHERE status != 'resolved')::int as open_tickets,
              COUNT(*) FILTER (WHERE priority IN ('high', 'urgent'))::int as high_priority_tickets,
              AVG(EXTRACT(EPOCH FROM (COALESCE(resolved_at, now()) - created_at)) / 3600)::numeric as avg_resolution_hours
            FROM tickets
            WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
            """,
            tenant_id,
            customer_id,
            since,
        )
        if not row:
            return {}
        
        return {
            "tickets_30d": int(row["tickets_30d"] or 0),
            "open_tickets": int(row["open_tickets"] or 0),
            "high_priority_tickets": int(row["high_priority_tickets"] or 0),
            "avg_resolution_hours": float(row["avg_resolution_hours"] or 0),
        }
    
    async def _extract_deal_features(self, tenant_id: str, customer_id: str) -> dict[str, Any]:
        """Extract features from deal history."""
        row = await self._conn.fetchrow(
            """
            SELECT
              COUNT(*)::int as total_deals,
              COUNT(*) FILTER (WHERE stage = 'closed_won')::int as won_deals,
              COUNT(*) FILTER (WHERE stage = 'closed_lost')::int as lost_deals,
              SUM(amount) FILTER (WHERE stage = 'closed_won')::numeric as total_revenue,
              AVG(EXTRACT(EPOCH FROM (updated_at - created_at)) / 86400)::numeric as avg_deal_cycle_days
            FROM deals
            WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
            """,
            tenant_id,
            customer_id,
        )
        if not row:
            return {}
        
        total = int(row["total_deals"] or 0)
        won = int(row["won_deals"] or 0)
        win_rate = won / total if total > 0 else 0.5
        
        return {
            "total_deals": total,
            "won_deals": won,
            "lost_deals": int(row["lost_deals"] or 0),
            "deal_win_rate": win_rate,
            "total_revenue": float(row["total_revenue"] or 0),
            "deal_velocity_days": float(row["avg_deal_cycle_days"] or 0),
        }
    
    async def _extract_payment_features(self, tenant_id: str, customer_id: str) -> dict[str, Any]:
        """Extract features from payment history."""
        row = await self._conn.fetchrow(
            """
            SELECT
              COUNT(*)::int as total_payments,
              COUNT(*) FILTER (WHERE status = 'completed')::int as successful_payments,
              COUNT(*) FILTER (WHERE status = 'failed')::int as failed_payments
            FROM payments
            WHERE tenant_id = $1::uuid AND customer_id = $2::uuid
            """,
            tenant_id,
            customer_id,
        )
        if not row:
            return {"payment_reliability": 1.0}
        
        total = int(row["total_payments"] or 0)
        successful = int(row["successful_payments"] or 0)
        reliability = successful / total if total > 0 else 1.0
        
        return {
            "total_payments": total,
            "successful_payments": successful,
            "failed_payments": int(row["failed_payments"] or 0),
            "payment_reliability": reliability,
        }
    
    def _calculate_engagement_score(self, features: dict[str, Any]) -> float:
        """Calculate engagement score (0-1)."""
        score = 0.5
        
        # Active tickets indicate engagement
        if features.get("tickets_30d", 0) > 0:
            score += 0.1
        if features.get("tickets_30d", 0) > 5:
            score += 0.1
        
        # Active deals indicate engagement
        if features.get("total_deals", 0) > 0:
            score += 0.15
        
        # Won deals boost engagement
        if features.get("won_deals", 0) > 0:
            score += 0.15
        
        return min(1.0, max(0.0, score))
    
    def _calculate_price_sensitivity(self, features: dict[str, Any]) -> float:
        """Calculate price sensitivity (0=not sensitive, 1=very sensitive)."""
        sensitivity = 0.5
        
        # Lost deals increase price sensitivity assumption
        lost = features.get("lost_deals", 0)
        if lost > 0:
            sensitivity += min(0.3, lost * 0.1)
        
        # High revenue customers tend to be less price sensitive
        revenue = features.get("total_revenue", 0)
        if revenue > 100000:
            sensitivity -= 0.2
        elif revenue > 50000:
            sensitivity -= 0.1
        
        return min(1.0, max(0.0, sensitivity))
    
    def _calculate_churn_sensitivity(self, features: dict[str, Any]) -> float:
        """Calculate churn risk sensitivity (0=low, 1=high)."""
        sensitivity = 0.3
        
        # High ticket volume may indicate frustration
        tickets = features.get("tickets_30d", 0)
        if tickets > 10:
            sensitivity += 0.3
        elif tickets > 5:
            sensitivity += 0.15
        
        # Open high-priority tickets increase churn risk
        high_priority = features.get("high_priority_tickets", 0)
        if high_priority > 0:
            sensitivity += min(0.3, high_priority * 0.1)
        
        # Payment failures increase churn risk
        payment_reliability = features.get("payment_reliability", 1.0)
        if payment_reliability < 0.8:
            sensitivity += 0.2
        
        return min(1.0, max(0.0, sensitivity))
    
    def _calculate_conversion_likelihood(self, features: dict[str, Any]) -> float:
        """Calculate conversion likelihood (0-1)."""
        likelihood = 0.5
        
        # Historical win rate is strong indicator
        win_rate = features.get("deal_win_rate", 0.5)
        likelihood = 0.3 + (win_rate * 0.5)
        
        # Fast deal cycles indicate higher likelihood
        velocity = features.get("deal_velocity_days", 30)
        if velocity and velocity < 15:
            likelihood += 0.1
        
        return min(1.0, max(0.0, likelihood))
    
    def _calculate_support_dependency(self, features: dict[str, Any]) -> float:
        """Calculate support dependency (0-1)."""
        tickets = features.get("tickets_30d", 0)
        
        # Normalize: 0 tickets = 0, 10+ tickets = 1
        dependency = min(1.0, tickets / 10.0)
        
        return dependency
    
    def _calculate_confidence(self, features: dict[str, Any]) -> float:
        """Calculate confidence in the profile based on data availability."""
        confidence = 0.3  # Base confidence
        
        # More data sources = higher confidence
        if features.get("total_deals", 0) > 0:
            confidence += 0.2
        if features.get("tickets_30d", 0) > 0:
            confidence += 0.15
        if features.get("total_payments", 0) > 0:
            confidence += 0.2
        if features.get("journey_stage"):
            confidence += 0.15
        
        return min(1.0, confidence)
