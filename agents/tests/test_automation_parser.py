from intelligence.automation.rule_parser import parse_rule
from intelligence.automation.automation_agent import _serialize_graph_output
from intelligence.automation.workflow_compiler import compile_workflow


def test_parse_invoice_overdue_rule_fallback():
    wf, warnings = parse_rule(llm=None, nl_rule_text="When invoice overdue by 7 days, notify finance and assign call task.")
    assert wf.trigger == "invoice_overdue"
    assert any(c.field == "days_overdue" and c.operator == ">=" and c.value == 7 for c in wf.conditions)
    assert any(getattr(a, "type", None) == "notify" and getattr(a, "role", None) == "finance" for a in wf.actions)
    assert warnings is not None


def test_compile_enforces_allowlist():
    wf, _ = parse_rule(llm=None, nl_rule_text="When invoice overdue by 7 days, notify finance and assign call task.")
    compiled = compile_workflow(wf)
    assert compiled.trigger_type in ("invoice_overdue", "customer_updated")
    assert isinstance(compiled.trigger_topics, list)
    assert all(a.get("type") in ("notify", "create_task", "propose_followup") for a in compiled.actions)


def test_serialize_graph_output_accepts_langgraph_mapping_result():
    workflow, _ = parse_rule(
        llm=None,
        nl_rule_text="When invoice overdue by 7 days, notify finance and assign call task.",
    )
    compiled = compile_workflow(workflow)

    result = _serialize_graph_output(
        {"workflow": workflow, "compiled": compiled, "warnings": ["fallback_used"]}
    )

    assert result.trigger_type == "invoice_overdue"
    assert result.workflow["trigger"] == "invoice_overdue"
    assert result.compiled["trigger_type"] == "invoice_overdue"
    assert result.warnings == ["fallback_used"]
