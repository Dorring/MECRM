"""
Behavior Model - Predicts customer behavior for simulation scenarios.

Uses rule-based scoring with feature weights, extensible to ML models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry import trace

logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class BehaviorPrediction:
    """Prediction result from behavior model."""
    prediction_type: str
    probability: float
    confidence: float
    explanation: str
    features_used: list[str]


class BehaviorModel:
    """Predicts customer behavioral outcomes."""
    
    def __init__(self, features: dict[str, Any]):
        self._features = features
    
    def predict_price_response(self, increase_percent: float) -> dict[str, float]:
        """
        Predict customer response to price increase.
        
        Returns probability distribution: {retain, negotiate, churn}
        """
        with tracer.start_as_current_span("behavior_model.predict_price_response") as span:
            span.set_attribute("increase_percent", increase_percent)
            
            # Base probabilities
            retain = 0.6
            negotiate = 0.25
            churn = 0.15
            
            # Adjust based on price sensitivity
            sensitivity = float(self._features.get("price_sensitivity", 0.5))
            
            # Higher sensitivity = lower retain, higher churn
            sensitivity_factor = (sensitivity - 0.5) * 2  # -1 to 1
            retain -= sensitivity_factor * 0.2
            churn += sensitivity_factor * 0.15
            negotiate += sensitivity_factor * 0.05
            
            # Adjust based on increase magnitude
            if increase_percent > 15:
                retain -= 0.15
                churn += 0.1
                negotiate += 0.05
            elif increase_percent > 10:
                retain -= 0.08
                churn += 0.05
                negotiate += 0.03
            
            # Adjust based on engagement
            engagement = float(self._features.get("engagement_score", 0.5))
            if engagement > 0.7:
                retain += 0.1
                churn -= 0.05
            elif engagement < 0.3:
                retain -= 0.1
                churn += 0.1
            
            # Adjust based on payment reliability
            payment_reliability = float(self._features.get("payment_reliability", 1.0))
            if payment_reliability < 0.8:
                churn += 0.1
                retain -= 0.1
            
            # Normalize to sum to 1.0
            total = retain + negotiate + churn
            retain = max(0.01, min(0.98, retain / total))
            negotiate = max(0.01, min(0.98, negotiate / total))
            churn = max(0.01, min(0.98, churn / total))
            
            # Re-normalize after clamping
            total = retain + negotiate + churn
            
            return {
                "retain": round(retain / total, 3),
                "negotiate": round(negotiate / total, 3),
                "churn": round(churn / total, 3),
            }
    
    def predict_feature_removal_response(self, feature_name: str) -> dict[str, float]:
        """
        Predict customer response to feature removal.
        
        Returns probability distribution: {accept, complain, churn}
        """
        with tracer.start_as_current_span("behavior_model.predict_feature_removal"):
            # Base probabilities
            accept = 0.5
            complain = 0.35
            churn = 0.15
            
            # High support dependency = more likely to complain
            support_dependency = float(self._features.get("support_dependency", 0))
            if support_dependency > 0.5:
                complain += 0.15
                accept -= 0.1
            
            # Low engagement = might not notice or care
            engagement = float(self._features.get("engagement_score", 0.5))
            if engagement < 0.3:
                accept += 0.15
                complain -= 0.1
            
            # High churn sensitivity = more likely to churn
            churn_sensitivity = float(self._features.get("churn_sensitivity", 0.5))
            if churn_sensitivity > 0.6:
                churn += 0.1
                accept -= 0.1
            
            # Normalize
            total = accept + complain + churn
            
            return {
                "accept": round(accept / total, 3),
                "complain": round(complain / total, 3),
                "churn": round(churn / total, 3),
            }
    
    def predict_renewal_response(self) -> dict[str, float]:
        """
        Predict customer response to contract renewal.
        
        Returns probability distribution: {renew, negotiate, decline}
        """
        with tracer.start_as_current_span("behavior_model.predict_renewal_response"):
            # Base probabilities
            renew = 0.6
            negotiate = 0.25
            decline = 0.15
            
            # High engagement = more likely to renew
            engagement = float(self._features.get("engagement_score", 0.5))
            if engagement > 0.7:
                renew += 0.15
                decline -= 0.1
            elif engagement < 0.3:
                renew -= 0.2
                decline += 0.15
            
            # High churn sensitivity = less likely to renew
            churn_sensitivity = float(self._features.get("churn_sensitivity", 0.5))
            if churn_sensitivity > 0.6:
                renew -= 0.15
                decline += 0.1
            
            # Historical win rate indicates likelihood
            conversion = float(self._features.get("conversion_likelihood", 0.5))
            renew += (conversion - 0.5) * 0.2
            
            # Normalize
            total = renew + negotiate + decline
            renew = max(0.01, min(0.98, renew / total))
            negotiate = max(0.01, min(0.98, negotiate / total))
            decline = max(0.01, min(0.98, decline / total))
            
            total = renew + negotiate + decline
            
            return {
                "renew": round(renew / total, 3),
                "negotiate": round(negotiate / total, 3),
                "decline": round(decline / total, 3),
            }
    
    def predict_upsell_response(self, upsell_value: float) -> dict[str, float]:
        """
        Predict customer response to upsell offer.
        
        Returns probability distribution: {accept, defer, decline}
        """
        with tracer.start_as_current_span("behavior_model.predict_upsell_response"):
            # Base probabilities
            accept = 0.3
            defer = 0.4
            decline = 0.3
            
            # High engagement = more likely to accept
            engagement = float(self._features.get("engagement_score", 0.5))
            if engagement > 0.7:
                accept += 0.2
                decline -= 0.15
            elif engagement < 0.3:
                accept -= 0.1
                decline += 0.15
            
            # High conversion likelihood = more likely to accept
            conversion = float(self._features.get("conversion_likelihood", 0.5))
            accept += (conversion - 0.5) * 0.2
            
            # Price sensitivity affects upsell acceptance
            price_sensitivity = float(self._features.get("price_sensitivity", 0.5))
            if price_sensitivity > 0.6:
                accept -= 0.1
                decline += 0.1
            
            # Large upsell value = lower acceptance
            if upsell_value > 10000:
                accept -= 0.15
                defer += 0.1
            
            # Normalize
            total = accept + defer + decline
            
            return {
                "accept": round(accept / total, 3),
                "defer": round(defer / total, 3),
                "decline": round(decline / total, 3),
            }
    
    def get_model_confidence(self) -> float:
        """Get overall model confidence based on available features."""
        return float(self._features.get("confidence", 0.5))
    
    def get_explanation_factors(self, scenario: str) -> list[str]:
        """Get key factors influencing the prediction."""
        factors = []
        
        engagement = float(self._features.get("engagement_score", 0.5))
        if engagement > 0.7:
            factors.append("High customer engagement historically")
        elif engagement < 0.3:
            factors.append("Low customer engagement signals")
        
        price_sensitivity = float(self._features.get("price_sensitivity", 0.5))
        if price_sensitivity > 0.6:
            factors.append("Customer shows price sensitivity patterns")
        elif price_sensitivity < 0.4:
            factors.append("Customer is less price-sensitive")
        
        churn_sensitivity = float(self._features.get("churn_sensitivity", 0.5))
        if churn_sensitivity > 0.6:
            factors.append("Elevated churn risk indicators")
        
        tickets = int(self._features.get("tickets_30d", 0))
        if tickets > 5:
            factors.append(f"High support activity ({tickets} tickets in 30d)")
        
        payment_reliability = float(self._features.get("payment_reliability", 1.0))
        if payment_reliability < 0.9:
            factors.append("Payment reliability concerns")
        
        return factors if factors else ["Baseline behavioral model applied"]
