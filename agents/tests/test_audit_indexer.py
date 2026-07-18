from intelligence.compliance.audit_indexer import _decision_text_blob


def test_decision_text_blob_accepts_json_strings_from_database_driver():
    blob = _decision_text_blob(
        {
            "agent_id": "automation",
            "action_type": "create_policy",
            "risk_level": "low",
            "status": "allowed",
            "reasoning": '{"factors":["tenant scoped"]}',
            "evidence": '{"source":"local demo"}',
            "tool_calls": '[{"name":"create_policy"}]',
        }
    )

    assert "factors=[\"tenant scoped\"]" in blob
    assert "evidence={\"source\": \"local demo\"}" in blob
    assert "tool_calls=[{\"name\": \"create_policy\"}]" in blob
