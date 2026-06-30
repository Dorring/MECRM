from pathlib import Path

from schema.schema_registry import SchemaRegistry


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_event_schema_versions_are_backward_compatible():
    registry = SchemaRegistry()
    registry.load_from_repo(root=str(REPO_ROOT))

    v1 = registry.get(event_type="lead.created", schema_version=1).schema
    v2 = registry.get(event_type="lead.created", schema_version=2).schema

    ok, reason = registry.is_backward_compatible(old=v1, new=v2)
    assert ok, reason


def test_breaking_change_removing_required_field_is_blocked():
    registry = SchemaRegistry()
    v1 = {
        "type": "object",
        "required": ["leadId", "name", "status"],
        "properties": {"leadId": {"type": "string"}, "name": {"type": "string"}, "status": {"type": "string"}},
    }
    v2_breaking = {
        "type": "object",
        "required": ["leadId", "status"],
        "properties": {"leadId": {"type": "string"}, "status": {"type": "string"}},
    }

    ok, _ = registry.is_backward_compatible(old=v1, new=v2_breaking)
    assert not ok

