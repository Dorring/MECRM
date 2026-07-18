from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import structlog
from intelligence.providers import create_chat_model
from opentelemetry import trace

from .graph import AutomationDeps, AutomationState, build_automation_graph


logger = structlog.get_logger()
tracer = trace.get_tracer(__name__)


@dataclass
class AutomationParseResponse:
    trigger_type: str
    workflow: dict[str, Any]
    compiled: dict[str, Any]
    warnings: list[str]


def _serialize_graph_output(out: AutomationState | Mapping[str, Any]) -> AutomationParseResponse:
    """Normalize LangGraph's mapping result before producing the HTTP response.

    LangGraph returns a mapping for ``ainvoke`` even when the graph state is a
    dataclass.  Keeping this conversion at the boundary makes the API stable
    for both direct graph use and LangGraph's serialized return value.
    """
    if isinstance(out, Mapping):
        workflow_value = out.get("workflow")
        compiled_value = out.get("compiled")
        warnings_value = out.get("warnings")
    else:
        workflow_value = out.workflow
        compiled_value = out.compiled
        warnings_value = out.warnings

    workflow = (
        workflow_value.model_dump()
        if workflow_value is not None and hasattr(workflow_value, "model_dump")
        else {"trigger": "customer_updated", "conditions": [], "actions": []}
    )
    compiled = (
        compiled_value.to_dict()
        if compiled_value is not None and hasattr(compiled_value, "to_dict")
        else {
            "trigger_type": "customer_updated",
            "trigger_topics": [],
            "conditions": [],
            "actions": [],
            "warnings": ["missing_compiled"],
        }
    )
    warnings = list(warnings_value) if isinstance(warnings_value, list) else []
    return AutomationParseResponse(
        trigger_type=str(
            compiled.get("trigger_type")
            or workflow.get("trigger")
            or "customer_updated"
        ),
        workflow=workflow,
        compiled=compiled,
        warnings=warnings,
    )


class AutomationAgent:
    def __init__(self):
        self._llm = create_chat_model(temperature=0)
        self._graph = build_automation_graph(deps=AutomationDeps(llm=self._llm))

    async def parse(self, *, tenant_id: str, user_id: str, roles: list[str], nl_rule_text: str) -> dict[str, Any]:
        with tracer.start_as_current_span("rule_parse") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("user_id", user_id)
            span.set_attribute("rule_len", len(nl_rule_text or ""))

            state = AutomationState(nl_rule_text=nl_rule_text)
            out: AutomationState | Mapping[str, Any] = await self._graph.ainvoke(state)
            return _serialize_graph_output(out).__dict__
