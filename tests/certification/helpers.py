import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[2]


def compose(args: list[str], *, timeout: int = 600) -> None:
    subprocess.run(["docker", "compose", *args], cwd=str(ROOT), check=True, timeout=timeout)


def ensure_infra(*, services: list[str]) -> None:
    compose(["up", "-d", *services], timeout=900)


def stop_service(name: str) -> None:
    compose(["stop", name], timeout=300)


def start_service(name: str) -> None:
    compose(["start", name], timeout=300)


def psql(db: str, sql: str, *, timeout: int = 180) -> None:
    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        os.environ.get("POSTGRES_USER", "crm_user"),
        "-d",
        db,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    subprocess.run(cmd, cwd=str(ROOT), input=sql.encode("utf-8"), check=True, timeout=timeout)


def apply_sql_file(db: str, path: Path) -> None:
    psql(db, path.read_text(encoding="utf-8"))


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}", re.IGNORECASE)
PHONE_CANDIDATE_RE = re.compile(r"(?<![0-9a-fA-F])[+()]?\\d[\\d\\s()/-]{8,}\\d(?![0-9a-fA-F])")
UUID_RE = re.compile(r"\\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\\b\\d{4}-\\d{2}-\\d{2}\\b")


@dataclass(frozen=True)
class PiiScanResult:
    emails: list[str]
    phones: list[str]

    @property
    def ok(self) -> bool:
        return not self.emails and not self.phones


def scan_for_pii(text: str) -> PiiScanResult:
    emails = sorted(set(EMAIL_RE.findall(text)))
    phones: set[str] = set()
    for cand in PHONE_CANDIDATE_RE.findall(text):
        if "." in cand:
            continue
        if ISO_DATE_RE.search(cand):
            continue
        if UUID_RE.search(cand):
            continue
        digits = sum(1 for ch in cand if ch.isdigit())
        if 10 <= digits <= 15:
            phones.add(cand)
    phones = sorted(phones)
    return PiiScanResult(emails=emails, phones=phones)


async def fetch_text(url: str, *, timeout_seconds: float = 5.0) -> str:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def scan_json_obj(obj: Any) -> PiiScanResult:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    return scan_for_pii(raw)


def scan_files_for_pii(paths: list[Path]) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for p in paths:
        if not p.exists() or not p.is_file():
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        result = scan_for_pii(raw)
        if result.ok:
            continue
        out[str(p)] = {"emails": result.emails, "phones": result.phones}
    return out


async def prom_query(*, base_url: str, promql: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/query"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"query": promql})
        resp.raise_for_status()
        return resp.json()


async def prom_rules(*, base_url: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/rules"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
