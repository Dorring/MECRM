from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from dataclasses import dataclass


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def chaos_enabled() -> bool:
    return os.getenv("CHAOS_TESTS_ENABLED", "").lower() in ("1", "true", "yes")


def chaos_environment() -> str:
    return os.getenv("CHAOS_ENVIRONMENT", "").lower()


def require_chaos_enabled() -> None:
    if not chaos_enabled():
        raise RuntimeError("CHAOS_TESTS_ENABLED is not true")
    if chaos_environment() not in ("local", "ci", "staging"):
        raise RuntimeError("CHAOS_ENVIRONMENT must be one of {local,ci,staging}")


def _run(cmd: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        timeout=timeout,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _docker_compose_prefix(compose_file: str) -> list[str]:
    try:
        _run(["docker", "compose", "version"], timeout=15)
        return ["docker", "compose", "-f", compose_file]
    except Exception:
        return ["docker-compose", "-f", compose_file]


def compose(compose_file: str, args: list[str], *, timeout: int = 240) -> str:
    result = _run(_docker_compose_prefix(compose_file) + args, timeout=timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"compose failed: {' '.join(args)}\n{result.stdout}")
    return result.stdout


def docker(args: list[str], *, timeout: int = 120) -> str:
    result = _run(["docker"] + args, timeout=timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"docker failed: {' '.join(args)}\n{result.stdout}")
    return result.stdout


def wait_for_container_healthy(container_name: str, *, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        raw = docker(["inspect", container_name, "--format", "{{json .State.Health}}"], timeout=30)
        try:
            data = json.loads(raw.strip())
        except Exception:
            time.sleep(1)
            continue
        if not data:
            return
        if data.get("Status") == "healthy":
            return
        time.sleep(1)
    raise RuntimeError(f"container not healthy: {container_name}")


@dataclass(frozen=True)
class ChaosEndpoints:
    postgres_dsn: str
    kafka_brokers: str
    redis_url: str


def endpoints() -> ChaosEndpoints:
    return ChaosEndpoints(
        postgres_dsn=os.getenv("CHAOS_DATABASE_URL", "postgresql://crm_user:crm_password@localhost:5432/enterprise_crm"),
        kafka_brokers=os.getenv("CHAOS_KAFKA_BROKERS", "localhost:9094"),
        redis_url=os.getenv("CHAOS_REDIS_URL", "redis://localhost:6379"),
    )


def report_dir() -> pathlib.Path:
    d = REPO_ROOT / "reports" / "chaos"
    d.mkdir(parents=True, exist_ok=True)
    return d
