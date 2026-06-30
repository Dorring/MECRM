from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx


def compute_roles_hash(*, roles: list[str]) -> str:
    normalized = [r.strip().lower() for r in roles if r and r.strip()]
    normalized.sort()
    payload = ",".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_policy_hash(*, input_obj: dict[str, Any]) -> str:
    payload = _canonical_json(input_obj).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    policy_hash: str
    raw: dict[str, Any] | None = None


class OpaClient:
    def __init__(self, opa_url: str, *, timeout_seconds: float = 2.0):
        self._opa_url = opa_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def evaluate(self, *, policy_path: str, input_obj: dict[str, Any]) -> PolicyDecision:
        policy_hash = compute_policy_hash(input_obj=input_obj)
        url = f"{self._opa_url}/v1/data/{policy_path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(url, json={"input": input_obj})
                resp.raise_for_status()
                body = resp.json()
        except Exception:
            return PolicyDecision(allow=False, policy_hash=policy_hash, raw=None)

        result = body.get("result")
        allow = bool(result.get("allow")) if isinstance(result, dict) else bool(result)
        return PolicyDecision(allow=allow, policy_hash=policy_hash, raw=body)

