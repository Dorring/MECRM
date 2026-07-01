from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DraftOutput:
    email_subject: str | None
    email_body: str | None
    whatsapp_message: str | None
    task_description: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "email": {"subject": self.email_subject, "body": self.email_body} if (self.email_subject or self.email_body) else None,
            "whatsapp": {"message": self.whatsapp_message} if self.whatsapp_message else None,
            "task": {"description": self.task_description} if self.task_description else None,
        }


def parse_drafts(raw: str) -> DraftOutput:
    obj = _parse_json_obj(raw)
    email_raw = obj.get("email")
    email = email_raw if isinstance(email_raw, dict) else {}
    whatsapp_raw = obj.get("whatsapp")
    whatsapp = whatsapp_raw if isinstance(whatsapp_raw, dict) else {}
    task_raw = obj.get("task")
    task = task_raw if isinstance(task_raw, dict) else {}
    return DraftOutput(
        email_subject=_s(email.get("subject")),
        email_body=_s(email.get("body")),
        whatsapp_message=_s(whatsapp.get("message")),
        task_description=_s(task.get("description")),
    )


def _parse_json_obj(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return {}
    return {}


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None

