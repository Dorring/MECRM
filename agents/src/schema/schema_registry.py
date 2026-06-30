import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SchemaError(Exception):
    pass


@dataclass(frozen=True)
class RegisteredSchema:
    event_type: str
    schema_version: int
    schema: dict[str, Any]


class SchemaRegistry:
    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], RegisteredSchema] = {}

    def register_schema(self, *, event_type: str, schema_version: int, schema: dict[str, Any]) -> None:
        self._schemas[(event_type, schema_version)] = RegisteredSchema(event_type, schema_version, schema)

    def load_from_repo(self, *, root: str | Path) -> None:
        root_path = Path(root)
        for vfile in root_path.glob("schemas/events/*/v*.json"):
            event_type = vfile.parent.name
            version_str = vfile.stem.lstrip("v")
            schema_version = int(version_str)
            schema = json.loads(vfile.read_text(encoding="utf-8"))
            self.register_schema(event_type=event_type, schema_version=schema_version, schema=schema)

    def get(self, *, event_type: str, schema_version: int) -> RegisteredSchema:
        key = (event_type, schema_version)
        if key not in self._schemas:
            raise SchemaError(f"Schema not found: {event_type} v{schema_version}")
        return self._schemas[key]

    def validate_payload(self, *, event_type: str, schema_version: int, payload: dict[str, Any]) -> None:
        schema = self.get(event_type=event_type, schema_version=schema_version).schema
        required = schema.get("required") or []
        if not isinstance(required, list):
            raise SchemaError("Invalid schema: required must be a list")
        for k in required:
            if k not in payload:
                raise SchemaError(f"Missing required field: {k}")

        props = schema.get("properties") or {}
        if not isinstance(props, dict):
            raise SchemaError("Invalid schema: properties must be an object")

        for k, spec in props.items():
            if k not in payload:
                continue
            if not isinstance(spec, dict):
                continue
            expected = spec.get("type")
            if expected is None:
                continue
            val = payload[k]
            if expected == "string" and not (val is None or isinstance(val, str)):
                raise SchemaError(f"Field {k} must be string")
            if expected == "integer" and not (val is None or isinstance(val, int)):
                raise SchemaError(f"Field {k} must be integer")
            if expected == "number" and not (val is None or isinstance(val, (int, float))):
                raise SchemaError(f"Field {k} must be number")
            if expected == "object" and not (val is None or isinstance(val, dict)):
                raise SchemaError(f"Field {k} must be object")
            if expected == "boolean" and not (val is None or isinstance(val, bool)):
                raise SchemaError(f"Field {k} must be boolean")

    def is_backward_compatible(self, *, old: dict[str, Any], new: dict[str, Any]) -> tuple[bool, str]:
        old_req = set(old.get("required") or [])
        new_req = set(new.get("required") or [])
        if old_req != new_req:
            return False, "required fields changed"

        old_props = old.get("properties") or {}
        new_props = new.get("properties") or {}
        if not isinstance(old_props, dict) or not isinstance(new_props, dict):
            return False, "invalid properties"

        for k in old_props.keys() & new_props.keys():
            ot = (old_props.get(k) or {}).get("type")
            nt = (new_props.get(k) or {}).get("type")
            if ot is not None and nt is not None and ot != nt:
                return False, f"type changed for {k}"

        return True, "ok"

