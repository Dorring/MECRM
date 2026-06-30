from intelligence.analytics.predictors import (
    predict_churn,
    predict_conversion,
    predict_escalation,
    predict_sla_breach,
    risk_level_from_probability,
)


def test_risk_level_mapping():
    assert risk_level_from_probability(0.0) == "green"
    assert risk_level_from_probability(0.29) == "green"
    assert risk_level_from_probability(0.3) == "yellow"
    assert risk_level_from_probability(0.59) == "yellow"
    assert risk_level_from_probability(0.6) == "red"
    assert risk_level_from_probability(1.0) == "red"


def test_churn_prediction_is_clamped_and_has_explanation():
    p = predict_churn(
        customer_id="c",
        stage="churn_risk",
        features={"overdue_tickets": 10, "open_high_tickets": 10, "tickets_30d": 50},
    )
    assert 0.0 <= p.probability <= 1.0
    assert p.explanation
    assert p.prediction_type == "churn"
    assert p.entity_type == "customer"


def test_ticket_sla_overdue_is_high_risk():
    p = predict_sla_breach(ticket_id="t", features={"ticket_id": "t", "priority": "urgent", "overdue": True})
    assert p.risk_level == "red"
    assert p.probability >= 0.6


def test_ticket_escalation_missing_data_is_safe():
    p = predict_escalation(ticket_id="t", features={"ticket_id": "t", "missing": True})
    assert p.risk_level == "green"
    assert p.probability == 0.0


def test_conversion_prediction_for_converted_is_high():
    p = predict_conversion(entity_type="customer", entity_id="c", stage="converted", features={"latest_deal_stage": "closed_won"})
    assert p.probability >= 0.8

