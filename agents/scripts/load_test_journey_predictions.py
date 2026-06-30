import argparse
import random
import time
from uuid import uuid4

from intelligence.analytics.predictors import predict_churn, predict_conversion, predict_escalation, predict_sla_breach


def run(*, customers: int) -> None:
    ids = [str(uuid4()) for _ in range(customers)]
    t0 = time.perf_counter()
    churn_levels = {"green": 0, "yellow": 0, "red": 0}

    for i, cid in enumerate(ids):
        features = {
            "overdue_tickets": random.randint(0, 2),
            "open_high_tickets": random.randint(0, 3),
            "tickets_30d": random.randint(0, 10),
            "latest_deal_stage": random.choice(["prospecting", "qualification", "proposal", "negotiation", ""]),
            "deal_age_days": random.random() * 60,
        }
        stage = random.choice(["awareness", "engaged", "negotiating", "hesitation", "converted", "churn_risk"])
        churn = predict_churn(customer_id=cid, stage=stage, features=features)
        churn_levels[churn.risk_level] += 1
        _ = predict_conversion(entity_type="customer", entity_id=cid, stage=stage, features=features)
        _ = predict_sla_breach(ticket_id=str(uuid4()), features={"ticket_id": "t", "priority": random.choice(["low", "medium", "high", "urgent"]), "overdue": random.choice([False, False, True])})
        _ = predict_escalation(ticket_id=str(uuid4()), features={"ticket_id": "t", "priority": random.choice(["low", "medium", "high", "urgent"]), "overdue": random.choice([False, True]), "age_hours": random.random() * 100})

        if i and i % 10000 == 0:
            pass

    dt = time.perf_counter() - t0
    rate = customers / dt
    print({"customers": customers, "seconds": dt, "customers_per_second": rate, "customers_per_hour": int(rate * 3600), "badge_distribution": churn_levels})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--customers", type=int, default=50_000)
    args = p.parse_args()
    run(customers=args.customers)


if __name__ == "__main__":
    main()

