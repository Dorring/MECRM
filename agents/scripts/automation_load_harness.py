import os
import random
import time
from typing import Any

from intelligence.automation.executor import _eval_condition


def _random_event() -> dict[str, Any]:
    return {
        "days_overdue": random.randint(0, 30),
        "amount": random.randint(100, 100000),
        "customer": {"tier": random.choice(["free", "pro", "enterprise"])},
        "status": random.choice(["open", "pending", "closed"]),
    }


def main() -> None:
    n = int(os.getenv("N_EVENTS", "100000"))
    conditions = [
        {"field": "days_overdue", "operator": ">=", "value": 7},
        {"field": "amount", "operator": ">=", "value": 5000},
    ]

    t0 = time.perf_counter()
    matched = 0
    for _ in range(n):
        e = _random_event()
        ok = True
        for c in conditions:
            if not _eval_condition(e, c):
                ok = False
                break
        if ok:
            matched += 1
    dt = time.perf_counter() - t0
    rate = int(n / dt) if dt > 0 else 0
    print({"events": n, "matched": matched, "seconds": round(dt, 3), "events_per_sec": rate})


if __name__ == "__main__":
    main()

