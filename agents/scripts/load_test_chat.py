import argparse
import asyncio
import time
from uuid import uuid4

import httpx


async def _one_conversation(*, client: httpx.AsyncClient, url: str, token: str, turns: int) -> float:
    conversation_id = str(uuid4())
    headers = {"Authorization": token if token.startswith("Bearer ") else f"Bearer {token}"}
    t0 = time.perf_counter()
    for i in range(turns):
        body = {"query": f"show recent leads (turn {i + 1})", "conversation_id": conversation_id, "mode": "chat"}
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
    return (time.perf_counter() - t0) * 1000.0


async def run(*, base_url: str, token: str, concurrency: int, turns: int) -> None:
    url = base_url.rstrip("/") + "/api/intelligence/query"
    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [_one_conversation(client=client, url=url, token=token, turns=turns) for _ in range(concurrency)]
        durations = await asyncio.gather(*tasks, return_exceptions=True)

    ok = [d for d in durations if isinstance(d, (int, float))]
    err = [d for d in durations if not isinstance(d, (int, float))]
    ok.sort()
    p50 = ok[int(len(ok) * 0.5)] if ok else None
    p95 = ok[int(len(ok) * 0.95)] if ok else None
    print(
        {
            "concurrency": concurrency,
            "turns": turns,
            "ok": len(ok),
            "errors": len(err),
            "p50_ms": p50,
            "p95_ms": p95,
        }
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:4000")
    p.add_argument("--token", required=True)
    p.add_argument("--concurrency", type=int, default=200)
    p.add_argument("--turns", type=int, default=3)
    args = p.parse_args()
    asyncio.run(run(base_url=args.base_url, token=args.token, concurrency=args.concurrency, turns=args.turns))


if __name__ == "__main__":
    main()

