from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ObjectStore:
    async def put_bytes(self, *, key: str, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError()

    async def get_bytes(self, *, key: str) -> bytes:  # pragma: no cover
        raise NotImplementedError()

    async def list_keys(self, *, prefix: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError()


@dataclass(frozen=True)
class LocalObjectStore(ObjectStore):
    base_dir: Path

    def __post_init__(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def put_bytes(self, *, key: str, data: bytes) -> None:
        path = self.base_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get_bytes(self, *, key: str) -> bytes:
        return (self.base_dir / key).read_bytes()

    async def list_keys(self, *, prefix: str) -> list[str]:
        root = self.base_dir / prefix
        if not root.exists():
            return []
        out: list[str] = []
        for p in root.rglob("*"):
            if p.is_file():
                out.append(str(p.relative_to(self.base_dir)).replace(os.sep, "/"))
        out.sort()
        return out


class S3CompatibleObjectStore(ObjectStore):
    def __init__(self, *, bucket: str, prefix: str = "", region: str | None = None):
        try:
            import boto3  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError("boto3 is required for S3CompatibleObjectStore") from e
        self._boto3 = boto3
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._region = region
        self._client = boto3.client("s3", region_name=region)

    def _key(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self._prefix}/{key}" if self._prefix else key

    async def put_bytes(self, *, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=self._key(key), Body=data)

    async def get_bytes(self, *, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
        return resp["Body"].read()

    async def list_keys(self, *, prefix: str) -> list[str]:
        p = self._key(prefix)
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": p}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for o in resp.get("Contents", []):
                k = o.get("Key")
                if isinstance(k, str):
                    keys.append(k)
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        keys.sort()
        return keys
