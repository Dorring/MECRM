from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .metrics import backup_duration_seconds
from .object_store import LocalObjectStore, ObjectStore


@dataclass(frozen=True)
class BackupArtifact:
    backup_id: str
    created_at: str
    keys: list[str]
    sha256: dict[str, str]
    metadata: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BackupService:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        object_store: ObjectStore | None = None,
        docker_compose_project_dir: str | None = None,
    ):
        self._pool = pool
        self._object_store = object_store or LocalObjectStore(base_dir=__import__("pathlib").Path("backups"))
        self._docker_compose_project_dir = docker_compose_project_dir

    async def create_db_backup(self, *, database_url: str, backup_id: str | None = None) -> BackupArtifact:
        backup_id = backup_id or str(uuid.uuid4())
        t0 = time.perf_counter()
        key = f"dr/{backup_id}/db.sql"

        dump = self._pg_dump(database_url)
        sha = _sha256(dump)
        await self._object_store.put_bytes(key=key, data=dump)

        manifest = BackupArtifact(
            backup_id=backup_id,
            created_at=_now_iso(),
            keys=[key],
            sha256={key: sha},
            metadata={"type": "db", "database_url_redacted": True},
        )
        await self._write_manifest(manifest)
        backup_duration_seconds.labels(type="db").observe(time.perf_counter() - t0)
        return manifest

    async def create_snapshot_backup(self, *, backup_id: str | None = None) -> BackupArtifact:
        backup_id = backup_id or str(uuid.uuid4())
        t0 = time.perf_counter()
        keys: list[str] = []
        sha_map: dict[str, str] = {}

        snap_key = f"dr/{backup_id}/aggregate_snapshots.jsonl"
        evlog_key = f"dr/{backup_id}/event_log.jsonl"

        snapshots = await self._export_table_jsonl(
            "SELECT tenant_id, aggregate_type, aggregate_id, version, ts, state, kafka_topic, kafka_partition, kafka_offset FROM aggregate_snapshots ORDER BY tenant_id, aggregate_type, aggregate_id, version"
        )
        await self._object_store.put_bytes(key=snap_key, data=snapshots)
        keys.append(snap_key)
        sha_map[snap_key] = _sha256(snapshots)

        event_log = await self._export_table_jsonl(
            "SELECT tenant_id, aggregate_type, aggregate_id, event_id, event_type, version, ts, payload FROM event_log ORDER BY tenant_id, aggregate_type, aggregate_id, version"
        )
        await self._object_store.put_bytes(key=evlog_key, data=event_log)
        keys.append(evlog_key)
        sha_map[evlog_key] = _sha256(event_log)

        manifest = BackupArtifact(
            backup_id=backup_id,
            created_at=_now_iso(),
            keys=keys,
            sha256=sha_map,
            metadata={"type": "snapshots"},
        )
        await self._write_manifest(manifest)
        backup_duration_seconds.labels(type="snapshots").observe(time.perf_counter() - t0)
        return manifest

    async def list_backups(self) -> list[BackupArtifact]:
        keys = await self._object_store.list_keys(prefix="dr")
        manifest_keys = [k for k in keys if k.endswith("/manifest.json")]
        out: list[BackupArtifact] = []
        for k in manifest_keys:
            raw = await self._object_store.get_bytes(key=k)
            obj = json.loads(raw.decode("utf-8"))
            out.append(BackupArtifact(**obj))
        out.sort(key=lambda m: m.created_at)
        return out

    async def _export_table_jsonl(self, query: str) -> bytes:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query)
        lines: list[str] = []
        for r in rows:
            lines.append(json.dumps(dict(r), default=str, separators=(",", ":"), ensure_ascii=False))
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    async def _write_manifest(self, manifest: BackupArtifact) -> None:
        key = f"dr/{manifest.backup_id}/manifest.json"
        data = json.dumps(
            {
                "backup_id": manifest.backup_id,
                "created_at": manifest.created_at,
                "keys": manifest.keys,
                "sha256": manifest.sha256,
                "metadata": manifest.metadata,
            },
            indent=2,
        ).encode("utf-8")
        await self._object_store.put_bytes(key=key, data=data)

    def _pg_dump(self, database_url: str) -> bytes:
        cmd = ["pg_dump", database_url, "--no-owner", "--no-acl", "--clean", "--if-exists"]
        try:
            return subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        except Exception:
            if self._docker_compose_project_dir:
                return subprocess.check_output(
                    ["docker", "compose", "exec", "-T", "postgres", "pg_dump", database_url, "--no-owner", "--no-acl", "--clean", "--if-exists"],
                    cwd=self._docker_compose_project_dir,
                    stderr=subprocess.DEVNULL,
                )
            raise
