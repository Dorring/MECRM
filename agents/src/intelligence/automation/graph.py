from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.graph import StateGraph

from .rule_parser import WorkflowSpec, parse_rule
from .workflow_compiler import CompiledWorkflow, compile_workflow


@dataclass
class AutomationState:
    nl_rule_text: str
    workflow: WorkflowSpec | None = None
    compiled: CompiledWorkflow | None = None
    warnings: list[str] | None = None


@dataclass(frozen=True)
class AutomationDeps:
    llm: Any


def build_automation_graph(*, deps: AutomationDeps):
    g: StateGraph = StateGraph(AutomationState)

    def _parse(state: AutomationState) -> AutomationState:
        workflow, warnings = parse_rule(llm=deps.llm, nl_rule_text=state.nl_rule_text)
        state.workflow = workflow
        state.warnings = warnings
        return state

    def _compile(state: AutomationState) -> AutomationState:
        if state.workflow is None:
            state.compiled = compile_workflow(WorkflowSpec(trigger="customer_updated"))
            state.warnings = (state.warnings or []) + ["missing_workflow_defaulted"]
            return state
        state.compiled = compile_workflow(state.workflow)
        state.warnings = (state.warnings or []) + list(state.compiled.warnings or [])
        return state

    g.add_node("parse", _parse)
    g.add_node("compile", _compile)
    g.set_entry_point("parse")
    g.add_edge("parse", "compile")
    g.set_finish_point("compile")
    return g.compile()

