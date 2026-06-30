import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core_services" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from governance.data_erasure import DataErasureService
from governance.retention_policy import RetentionPolicyEngine
from write.db import create_db_pool


async def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    pool = await create_db_pool(database_url)
    try:
        erasure = DataErasureService(pool)
        engine = RetentionPolicyEngine(pool, erasure=erasure)
        result = await engine.apply_policies()

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        out_dir = os.path.join(repo_root, "reports", "compliance")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "retention_policy.json")

        report = {"phase": "retention_policy", "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "result": result}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")

        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
