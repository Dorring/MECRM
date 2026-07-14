from pathlib import Path
import json
import re


ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_gateway_uses_prisma_7_pg_adapter_without_esm_uuid_migration():
    package = json.loads(read("gateway/package.json"))
    assert package["dependencies"]["@prisma/client"].startswith("^7.")
    assert package["devDependencies"]["prisma"].startswith("^7.")
    assert package["dependencies"]["@prisma/adapter-pg"].startswith("^7.")
    assert package["dependencies"]["pg"].startswith("^8.")
    assert "uuid" not in package["dependencies"]
    uuid_util = read("gateway/src/utils/uuid.ts")
    assert "randomUUID" in uuid_util
    assert "from 'crypto'" in uuid_util
    assert "type" not in package
    assert package["engines"]["node"].startswith(">=24")


def test_prisma_schema_moves_url_to_config_and_uses_js_engine():
    schema = read("gateway/prisma/schema.prisma")
    assert 'provider     = "prisma-client"' in schema
    assert 'output       = "../src/generated/prisma"' in schema
    assert 'moduleFormat = "cjs"' in schema
    assert 'engineType   = "client"' in schema
    assert 'url      = env("DATABASE_URL")' not in schema
    config = read("gateway/prisma.config.ts")
    assert "process.env.DATABASE_URL" in config
    assert "schema: 'prisma/schema.prisma'" in config
    assert "path: 'prisma/migrations'" in config


def test_prisma_client_receives_adapter_and_preserves_tenant_transaction():
    service = read("gateway/src/services/prisma.ts")
    assert "const adapter = new PrismaPg" in service
    assert "from '../generated/prisma/client'" in service
    assert re.search(r"new PrismaClient\(\{\s*adapter,", service)
    assert "prisma.$transaction" in service
    assert "set_config('app.tenant_id'" in service


def test_gateway_image_generates_once_and_compiles_generated_client():
    dockerfile = read("gateway/Dockerfile")
    assert dockerfile.count("RUN npx prisma generate") == 1
    assert "COPY prisma.config.ts ./" in dockerfile
    assert "/app/node_modules/.prisma" not in dockerfile
    assert "COPY --from=builder /app/dist ./dist" in dockerfile


def test_migration_image_contains_prisma_config():
    dockerfile = read("database/Dockerfile.migrate")
    assert "COPY gateway/prisma.config.ts ./prisma.config.ts" in dockerfile


def test_github_workflows_use_node_24_not_node_20():
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml")
    )
    assert 'node-version: "20"' not in workflows
    assert 'node-version: "24"' in workflows


def test_tenant_isolation_generates_client_before_direct_jest_run():
    workflow = read(".github/workflows/tenant-isolation.yml")
    generate = workflow.index("npm run prisma:generate")
    migrate = workflow.index("npx prisma migrate deploy")
    jest = workflow.index("npx jest tests/test_rls_enforcement.ts")
    assert generate < migrate < jest
