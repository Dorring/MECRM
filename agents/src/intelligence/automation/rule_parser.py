from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError


class Condition(BaseModel):
    field: str
    operator: Literal["==", "!=", ">", ">=", "<", "<=", "contains", "in"]
    value: Any


class NotifyAction(BaseModel):
    type: Literal["notify"]
    role: str
    message: str | None = None


class CreateTaskAction(BaseModel):
    type: Literal["create_task"]
    task: str
    assignee_role: str | None = None
    priority: Literal["low", "medium", "high"] = "medium"


class ProposeFollowupAction(BaseModel):
    type: Literal["propose_followup"]
    entity_type: Literal["lead", "deal", "customer", "ticket"]
    entity_id_field: str | None = None
    note: str | None = None


Action = NotifyAction | CreateTaskAction | ProposeFollowupAction


class WorkflowSpec(BaseModel):
    trigger: str
    conditions: list[Condition] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)


def _strip_code_fences(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _extract_json_object(text: str) -> str | None:
    raw = _strip_code_fences(text)
    if not raw:
        return None
    try:
        json.loads(raw)
        return raw
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    candidate = m.group(0)
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        return None


def parse_with_llm(*, llm: Any, nl_rule_text: str) -> tuple[WorkflowSpec | None, list[str]]:
    warnings: list[str] = []
    prompt = (
        "You are a workflow rule parser.\n"
        "Convert the user's rule into a STRICT JSON object with keys: trigger, conditions, actions.\n"
        "Constraints:\n"
        '- trigger: snake_case string like "invoice_overdue", "ticket_updated", "deal_updated", "prediction_generated".\n'
        '- conditions: array of {field, operator, value}. field is snake_case.\n'
        '- actions: array of objects. Allowed action types:\n'
        '  1) {"type":"notify","role":"<role>","message":"<optional>"}\n'
        '  2) {"type":"create_task","task":"<task>","assignee_role":"<optional>","priority":"low|medium|high"}\n'
        '  3) {"type":"propose_followup","entity_type":"lead|deal|customer|ticket","entity_id_field":"<optional>","note":"<optional>"}\n'
        "Return only valid JSON. No markdown.\n"
        f'User rule: "{nl_rule_text}"\n'
    )
    try:
        resp = llm.invoke(prompt)
        content = getattr(resp, "content", None) or str(resp)
        obj = _extract_json_object(content)
        if not obj:
            warnings.append("llm_no_json")
            return None, warnings
        data = json.loads(obj)
        wf = WorkflowSpec.model_validate(data)
        return wf, warnings
    except ValidationError as ve:
        warnings.append("llm_validation_failed")
        warnings.append(str(ve)[:400])
        return None, warnings
    except Exception:
        warnings.append("llm_failed")
        return None, warnings


def parse_fallback(*, nl_rule_text: str) -> tuple[WorkflowSpec, list[str]]:
    text = (nl_rule_text or "").strip()
    lower = text.lower()
    warnings: list[str] = []

    trigger = "customer_updated"
    if "invoice" in lower and "overdue" in lower:
        trigger = "invoice_overdue"
    elif "ticket" in lower:
        trigger = "ticket_updated"
    elif "deal" in lower:
        trigger = "deal_updated"
    elif "prediction" in lower:
        trigger = "prediction_generated"

    conditions: list[Condition] = []
    m = re.search(r"overdue\s+by\s+(\d+)\s+day", lower)
    if m:
        days = int(m.group(1))
        conditions.append(Condition(field="days_overdue", operator=">=", value=days))
    elif "overdue" in lower and "day" in lower:
        warnings.append("fallback_overdue_days_unknown")

    actions: list[Action] = []
    m_notify = re.search(r"notify\s+([a-zA-Z_ -]+)", lower)
    if m_notify:
        role = m_notify.group(1).split(" and ")[0].strip().replace(" ", "_")
        actions.append(NotifyAction(type="notify", role=role))
    if "assign" in lower and "task" in lower:
        task = "Call customer"
        m_task = re.search(r"assign\s+([a-zA-Z0-9 _-]+)\s+task", text, re.IGNORECASE)
        if m_task:
            task = m_task.group(1).strip()
        actions.append(CreateTaskAction(type="create_task", task=task, priority="medium"))

    if not actions:
        warnings.append("fallback_no_actions_detected")

    return WorkflowSpec(trigger=trigger, conditions=conditions, actions=actions), warnings


def parse_rule(*, llm: Any | None, nl_rule_text: str) -> tuple[WorkflowSpec, list[str]]:
    if llm:
        wf, warnings = parse_with_llm(llm=llm, nl_rule_text=nl_rule_text)
        if wf:
            return wf, warnings
    wf, w2 = parse_fallback(nl_rule_text=nl_rule_text)
    return wf, w2

