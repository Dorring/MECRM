"""Phase 5B — Trusted Execution Receipt.

The :class:`ActionExecutionReceipt` is the tamper-evident audit record
for ONE action execution.  It binds the adapter outcome back to the
:class:`ExecutionCommand`, the :class:`ExecutionAuthorization`, and
(optionally) the :class:`ApprovalDecision` that authorised it.

``receipt_hash`` covers EVERY field so a tampered receipt (e.g. a
swapped ``external_reference`` or a flipped ``executed`` flag) is
detected at the boundary.  :meth:`verify_against_command` and
:meth:`verify_against_authorization` bind the receipt back to its
inputs so a replayed or mis-routed receipt is fail-closed.

Invariants (Phase 5B Section 12):

* ``SUCCEEDED`` → ``executed=True``.
* ``FAILED`` → ``executed=False``.
* ``UNKNOWN`` / ``CANCELLED`` → ``executed=None``.
* ``started_at`` <= ``completed_at``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hmac import compare_digest
from typing import Any

from pydantic import ConfigDict, field_validator, model_validator

from multi_agent.action_adapter import ExecutionCommand
from multi_agent.contracts import StrictContract
from multi_agent.execution_authorization import (
    ExecutionAuthorization,
    ExecutionStatus,
)
from multi_agent.execution_error_codes import ExecutionReceiptError
from multi_agent.review_contracts import FrozenJsonValue, freeze_json_value
from multi_agent.serialization import stable_hash


class ActionExecutionReceipt(StrictContract):
    """Frozen, hash-stable receipt for one action execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str
    command_id: str
    tenant_id: str
    run_id: str
    proposal_id: str
    authorization_hash: str
    approval_decision_hash: str | None = None
    adapter_id: str
    adapter_version: str
    adapter_registry_hash: str
    idempotency_key: str
    execution_fingerprint: str
    status: ExecutionStatus
    executed: bool | None
    external_reference: str | None = None
    safe_result_summary: FrozenJsonValue = None
    started_at: datetime
    completed_at: datetime
    attempt: int = 1
    error_code: str | None = None
    receipt_hash: str = ""

    @field_validator(
        "receipt_id",
        "command_id",
        "tenant_id",
        "run_id",
        "proposal_id",
        "authorization_hash",
        "adapter_id",
        "adapter_version",
        "adapter_registry_hash",
        "execution_fingerprint",
    )
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ActionExecutionReceipt identity fields must not be blank")
        return v

    @field_validator("safe_result_summary")
    @classmethod
    def _freeze_summary(cls, v: Any) -> Any:
        return freeze_json_value(v)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ActionExecutionReceipt timestamps must be tz-aware")
        return v.astimezone(timezone.utc)

    @field_validator("attempt")
    @classmethod
    def _attempt_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("attempt must be >= 1")
        return v

    @model_validator(mode="after")
    def _verify_receipt_invariants(self) -> ActionExecutionReceipt:
        # status ↔ executed consistency.
        s = self.status
        if s == ExecutionStatus.SUCCEEDED and self.executed is not True:
            raise ValueError(
                f"Receipt {self.receipt_id!r}: SUCCEEDED requires executed=True"
            )
        if s == ExecutionStatus.FAILED and self.executed is not False:
            raise ValueError(
                f"Receipt {self.receipt_id!r}: FAILED requires executed=False"
            )
        if s in (ExecutionStatus.UNKNOWN, ExecutionStatus.CANCELLED):
            if self.executed is not None:
                raise ValueError(
                    f"Receipt {self.receipt_id!r}: {s.value!r} requires executed=None"
                )
        # started_at <= completed_at.
        if self.started_at > self.completed_at:
            raise ValueError(f"Receipt {self.receipt_id!r}: started_at > completed_at")
        # Populate / verify the receipt hash.
        expected = self.compute_hash()
        if not self.receipt_hash:
            object.__setattr__(self, "receipt_hash", expected)
        elif not compare_digest(self.receipt_hash, expected):
            raise ValueError(f"Receipt {self.receipt_id!r}: receipt_hash mismatch")
        return self

    def compute_hash(self) -> str:
        """Stable SHA-256 over every field except ``receipt_hash``."""
        return stable_hash(self, exclude={"receipt_hash"})

    def verify_integrity(self) -> None:
        """Recompute and compare ``receipt_hash``."""
        if not compare_digest(self.receipt_hash, self.compute_hash()):
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: receipt_hash does not match "
                f"recomputed content"
            )

    def verify_against_command(self, command: ExecutionCommand) -> None:
        """Bind the receipt back to the command that produced it.

        Verifies ``command_id``, ``adapter_id``, ``adapter_version``,
        ``execution_fingerprint``, ``idempotency_key``, ``attempt``
        and ``status`` consistency with the command's dry-run flag.
        """
        if self.command_id != command.command_id:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: command_id "
                f"{self.command_id!r} != command {command.command_id!r}"
            )
        if self.adapter_id != command.adapter_id:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: adapter_id mismatch"
            )
        if self.adapter_version != command.adapter_version:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: adapter_version mismatch"
            )
        if self.execution_fingerprint != command.execution_fingerprint:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: execution_fingerprint mismatch"
            )
        if self.idempotency_key != command.authorization.idempotency_key:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: idempotency_key mismatch"
            )
        if self.attempt != command.attempt:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: attempt "
                f"{self.attempt!r} != command {command.attempt!r}"
            )
        if self.authorization_hash != command.authorization.authorization_hash:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: authorization_hash mismatch"
            )

    def verify_against_authorization(self, auth: ExecutionAuthorization) -> None:
        """Bind the receipt back to its authorisation.

        Verifies ``authorization_hash``, ``tenant_id``, ``run_id``,
        ``proposal_id``, ``adapter_registry_hash``, ``idempotency_key``,
        and ``approval_decision_hash`` (when the authorization required
        approval).
        """
        if self.authorization_hash != auth.authorization_hash:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: authorization_hash "
                f"{self.authorization_hash[:12]!r} != auth "
                f"{auth.authorization_hash[:12]!r}"
            )
        if self.tenant_id != auth.tenant_id:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: tenant_id mismatch"
            )
        if self.run_id != auth.run_id:
            raise ExecutionReceiptError(f"Receipt {self.receipt_id!r}: run_id mismatch")
        if self.proposal_id != auth.proposal_id:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: proposal_id mismatch"
            )
        if self.adapter_registry_hash != auth.adapter_registry_hash:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: adapter_registry_hash mismatch"
            )
        if self.idempotency_key != auth.idempotency_key:
            raise ExecutionReceiptError(
                f"Receipt {self.receipt_id!r}: idempotency_key mismatch"
            )
        # If the authorization required approval, the receipt MUST
        # carry the matching approval_decision_hash.
        if auth.approval_required:
            if not self.approval_decision_hash:
                raise ExecutionReceiptError(
                    f"Receipt {self.receipt_id!r}: authorization required "
                    f"approval but receipt has no approval_decision_hash"
                )
            if auth.approval_decision_hash is None:
                raise ExecutionReceiptError(
                    f"Receipt {self.receipt_id!r}: authorization has no "
                    f"approval_decision_hash"
                )
            if self.approval_decision_hash != auth.approval_decision_hash:
                raise ExecutionReceiptError(
                    f"Receipt {self.receipt_id!r}: approval_decision_hash mismatch"
                )


__all__ = ["ActionExecutionReceipt"]
