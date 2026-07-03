# 批次 1 集中审查与验收报告

> 日期：2026-06-30
> 范围：7 个工作流（Gateway 认证 / CI-CD / Compose / Helm / Frontend / Agents / 数据库迁移）的两轮独立模型审查 + 真实运行验证
> 协议：`docs/project-review-protocol.md` · 计划：`docs/project-optimization-plan.md`

## 1. 真实运行验证结果

代码级验证在本地 Windows + Python/Node 环境实跑；Docker 级验证在 Docker Desktop 实跑（由用户完成，结果经复核）。

| 验证项 | 命令 | 结果 |
|---|---|---|
| Gateway lint | `npm run lint` | ✅ 0 错误 0 警告 |
| Gateway build | `npm run build` (prisma generate + tsc) | ✅ 通过 |
| Gateway test | `npm test` | ✅ 22 passed / 30 skipped [requires DB] / 0 failed |
| Frontend lint | `npm run lint` (eslint 9 flat config) | ✅ 0 错误 |
| Frontend tsc | `npx tsc --noEmit` | ✅ 0 错误 |
| Frontend build | `npm run build` | ✅ 通过，18 路由含 /login /settings |
| Agents pytest（全量） | `pytest tests/` | ✅ 138 passed / 37 skipped [requires DB] / 0 failed / 0 collection error |
| Infra 配置测试 | `pytest tests/infra/` | ✅ 32 passed + 10 subtests（含负向验证） |
| Compose config | `docker compose config` | ✅ 通过（Docker Desktop） |
| DB 空库迁移 | `docker compose --profile migrate run --rm migrate` | ✅ 通过（Prisma → SQL 01-11 → RLS 校验全绿） |
| DB 重复迁移 | 二次 run migrate | ✅ 幂等 |
| Stack smoke | `docker compose --profile smoke-test run --rm smoke-test` | ✅ 通过（注册→登录→建 Lead→列表→401） |
| Redis 停机认证 | 有效 token + Redis down | ✅ 401 fail-closed（P0-1 有效） |
| 跨租户隔离 | `test-gateway` 服务跑 RLS enforcement | ✅ 通过 |
| Kafka 停机/Outbox 清零 | — | ⏸️ **移至 Iteration 3**（Lead 仍直发 Kafka，无 Outbox Publisher，不冒充 Batch 1 能力） |

## 2. 本次阶段已修复

| 项 | 文件 | 修复 |
|---|---|---|
| WebSocket JWT 默认密钥（P0） | `gateway/src/services/websocket.ts:49` | 删除 `process.env.JWT_SECRET \|\| 'development-secret-change-in-production'`，改用 `config/jwt.ts` 的 `JWT_SECRET`，与 HTTP 路径统一 |
| Gateway unused-import 警告 | `gateway/src/index.ts:57` | 改为 side-effect import `import './config/jwt'`，保留启动期校验 |
| Next.js / eslint 版本漂移 | `frontend/package.json`, `eslint.config.mjs` | eslint 8→9, eslint-config-next 14.0.4→16.2.9, `.eslintrc.json`→flat config, `next lint`→`eslint .` |

禁用 3 条 eslint-plugin-react-hooks v5 新实验性规则（`immutability`/`set-state-in-effect`/`purity`），均标注为 Phase 5 P2 技术债，成熟规则（rules-of-hooks/exhaustive-deps）保持启用。

## 3. P0 问题清单（阻止发布）

| # | 文件:行号 | 问题 | 状态 |
|---|---|---|---|
| P0-1 | `gateway/src/services/websocket.ts:49` | WebSocket 鉴权绕过集中式 JWT 校验，回退硬编码默认密钥 | ✅ 已修复 |
| P0-2 | `docker-compose.yml:430` | migrate 服务缺 psql 二进制 + SQL 文件挂载，"one-shot migration runner" 无法运行，RLS 永不执行 | ✅ 已修复（migrate 改用 postgres 镜像 + 挂载 migrations + 全序列 01-11 + ON_ERROR_STOP + 依赖 postgres service_healthy） |
| P0-3 | `deploy/helm/.../templates/gateway.yaml:49,54` + `agents.yaml:48` | secretKeyRef `key: connection-string` 与 values `passwordKey: password` 不匹配 → Pod CrashLoopBackOff | ✅ 已修复（values 重命名 connectionStringKey，模板用 `{{ .Values.secrets.*.connectionStringKey }}`） |
| P0-4 | `.github/workflows/ci-cd.yml:216-250` | helm-lint job 未 `helm dependency build`，子 chart 缺失 → helm lint/template 必失败 | ✅ 已修复（加 `helm dependency build` + staging/production 各加 `helm template`） |
| P0-5 | `docker-compose.yml:155` | agents command `python -m src.orchestrator.main` 与 Dockerfile `python -m orchestrator.main` 不一致 | ✅ 已修复（compose command 改为 `orchestrator.main`，与 Dockerfile 一致） |
| P0-6 | `docker-compose.yml:202,206` + `agents/Dockerfile` | healthcheck `httpx.get()` 不检查状态码 → HTTP 500 仍判 healthy | ✅ 已修复（改为 `r.is_success` 判定，HTTP 500 现判 unhealthy） |
| P0-7 | `agents/src/agents/analytics.py:161-193` + `router.py:424-464` | forecast LLM 输出未经 Pydantic 校验直接 emit | ✅ 已修复（新增 ForecastPrediction/ForecastResult Pydantic 模型，model_validate 后才返回，ValidationError 返回 failed 且 router 不 emit） |
| P0-8 | `agents/src/orchestrator/main.py:131-146` | DLQ 发送失败仍提交 offset → 消息永久丢失 | ✅ 已修复（DLQ 失败不提交 offset，有界重试 + 指数退避至 DLQ_MAX_RETRIES，耗尽才推进并记 CRITICAL） |
| P0-9 | `agents/src/orchestrator/main.py:151-226` | DLQ 封包含未脱敏原始消息（PII） | ✅ 已修复（新增 `_redact_pii` 正则脱敏 email/SSN/credit_card/phone，original_value 与 error_reason 均脱敏，保留租户上下文） |

回归测试：P0-2~P0-6 → `tests/infra/`（13 测试，含负向验证）；P0-7~P0-9 → `agents/tests/test_agents.py`（10 新测试：forecast 校验 4 + router emit 门控 2 + DLQ 重试 2 + DLQ PII 脱敏 2）。

## 4. P1 问题清单（阻止生产）

| # | 文件:行号 | 问题 | 状态 |
|---|---|---|---|
| P1-1 | `gateway/src/index.ts:138` | `/ready` 探针未认证且返回 `String(error)`，泄露 DATABASE_URL/REDIS_URL/broker 凭据与拓扑 | 未修复 |
| P1-2 | `docker-compose.yml:407,411,459` + `.env.example:56` | Keycloak/Grafana 硬编码 admin/admin；.env.example JWT_SECRET 为弱默认值 | 未修复 |
| P1-3 | `docker-compose.yml:158-165` + `agents/config.py:57` | agents 容器未注入 GATEWAY_URL → 回退 localhost:4000，CrmReader 必失败 | ✅ 已修复（compose agents 环境加 GATEWAY_URL=http://gateway:4000，infra 测试断言） |
| P1-4 | `docker-compose.yml:48,174,176` | gateway/agents 对 opa/weaviate 用 service_started 而非 service_healthy | 未修复 |
| P1-5 | `.env.example` | 缺 REPLAY_DATABASE_URL / ENABLE_REPLAY_INGESTOR / GATEWAY_URL / OLLAMA_EMBED_MODEL 等变量 | 未修复 |
| P1-6 | `database/migrations/10-data-governance.sql:55-56` | RLS policy 用 `current_setting('app.tenant_id', true)::text` 与其他表 `::uuid` 不一致，语义分裂 | ✅ 已修复（policy 改为 UUID 比较；id 加 `DEFAULT gen_random_uuid()`）见 `hardening/db-migration` |
| P1-7 | `.github/workflows/ci-cd.yml` test-gateway | 仅 prisma migrate deploy，不跑 SQL migrations → CI 测试库无 RLS/event_log/twins 表 | ✅ 已修复（CI 与 smoke 均执行完整 Prisma→SQL→RLS） |
| P1-8 | `gateway/prisma/migrations/20260131062707_init` | 文件名暗示初始化，实为 DROP/ALTER/RENAME，误导 | ⏳ 未修复，已明确列入 Group G backlog：补迁移说明/基线策略，不得改写已发布 migration |
| P1-9 | Prisma `TIMESTAMP(3)` vs SQL `timestamptz` | 共享表时间列类型在两轨道间不一致 | ✅ 已修复（Prisma 字段加 `@db.Timestamptz(6)`；新增 forward migration `20260702000000_timestamptz_convergence`；SQL guard `12-type-convergence.sql`）见 `hardening/db-migration` |
| P1-10 | `agents/src/agents/base.py:131-157` | data_guard 对无 customerId/userId 的 LLM 衍生事件形同虚设 | 未修复（设计边界，待文档说明） |
| P1-11 | `agents/tests/test_event_store.py:9` | `from write.db import ...` 破损导入，全量 pytest 收集中断 | ✅ 已修复（sys.path.append core_services/src，collection 通过；DB 不可达时 skip） |
| P1-12 | `agents/tests/test_i18n.py` | 3 失败：langdetect/fasttext 未在 requirements.txt | ✅ 已修复（requirements.txt 加 langdetect>=1.0.9，fasttext 标可选；24 i18n 用例全通过） |
| P1-13 | Gateway Jest | 33 测试无 DB 时全 skip → CI 可能绿灯但未真实执行 | ✅ 已修复（纯逻辑解耦 DB 守卫；新增 nodb_security 10 测试 + leads_mocked 8 测试；21 passed，30 skipped 全标注 [requires DB]） |

## 5. P2 问题清单（稳定化）

- `gateway/src/index.ts:360-364` — uncaughtException 无法捕获 JWT_SECRET 模块加载期抛错（时序），脱敏日志意图未生效
- `gateway/src/services/websocket.ts:38-78` — WS 鉴权不查 token 黑名单，注销后 token 仍可建 WS
- `gateway/src/middleware/auth.ts:52` — 黑名单 key 用整条 token，无法按用户批量撤销
- `scripts/migrate.sh` / `migrate.ps1` — ✅ 已修复（session-level advisory lock）见 `hardening/db-migration`
- `scripts/migrate.sh:190-194` — ✅ 已修复（强制 RLS audit；`--audit-warn` 仅开发用）见 `hardening/db-migration`
- Helm values — opa/weaviate/ollama/monitoring/secrets.keycloak 声明但无模板消费；agents.yaml 硬编码服务名
- `docker-compose.yml:222` — postgres 挂载 `database/init/01-init.sql.disabled`（地雷文件，RLS 合约错误）
- `docker-compose.yml:284` — Kafka `AUTO_CREATE_TOPICS_ENABLE=true`（生产反模式）
- `database/migrations/02-rls-policies.sql` — ✅ 已修复（tenant_tables 数组扩展至全部租户表）见 `hardening/db-migration`
- `agents/src/orchestrator/main.py:162-169` — 非 JSON poison message tenant="" 可能在 DLQ 堆积
- `agents/src/agents/support.py:96-116` — triage_ticket LLM 输出未强校验（与 suggest_resolution 不一致）
- `agents/src/orchestrator/router.py:443-464` — forecast 重 emit 的 prediction_type 不在 productivity projector 白名单 → 静默忽略
- `docker-compose.yml:338` — OPA `/health` 端点未启用 `--health` 时可能 404；opa/ollama/weaviate 镜像可能无 wget
- `frontend` — NEXT_PUBLIC_API_URL=localhost:4000 构建期内联，非 localhost 部署失败；next.config.js rewrites 未被使用
- `.github/workflows/ci-cd.yml:234-250` — helm-lint 对 staging/production 只 lint 不 template
- `.github/workflows/chaos-tests.yml` — 启动 chaos compose 后未跑 migrate，测试库可能为空

## 6. 验收门槛状态

| 门槛 | 状态 | 说明 |
|---|---|---|
| Gateway build/test | ✅ 通过 | build ✅；test 22 passed / 30 skipped [requires DB] / 0 failed |
| Frontend lint/build | ✅ 通过 | eslint 9 + eslint-config-next 16 + flat config |
| Agents pytest | ✅ 通过 | 138 passed / 37 skipped [requires DB] / 0 failed / 0 collection error |
| Infra 配置测试 | ✅ 通过 | tests/infra/ 32 测试（含负向验证） |
| P0-1~P0-9 全部关闭 | ✅ 完成 | 9/9 P0 已修复并配回归测试 |
| P1-11/12/13 关闭 | ✅ 完成 | 验收可信度 P1 已修复 |
| WebSocket JWT 默认密钥清除 | ✅ 完成 | |
| 依赖版本漂移固定 | ✅ 完成 | |
| Compose config | ✅ 通过 | Docker Desktop |
| PostgreSQL 空库迁移 + 重复迁移 | ✅ 通过 | Docker Desktop（Prisma → SQL 01-11 → RLS 校验全绿） |
| 基础 smoke test | ✅ 通过 | Docker Desktop（真实认证写入/读取/401） |
| Redis 停机认证（fail-closed） | ✅ 通过 | Docker Desktop（有效 token + Redis down → 401） |
| 跨租户隔离 | ✅ 通过 | Docker Desktop（test-gateway 跑 RLS enforcement） |
| Kafka 停机/Outbox 清零 | ⏸️ 移至 Iteration 3 | Lead 仍直发 Kafka，无 Outbox Publisher；作为 Outbox 退出条件 |
| CI 与本地验收一致 | ✅ 修复 | ci-cd.yml smoke job 执行 Prisma→SQL→RLS→Gateway tests→Smoke |
| 错误信息脱敏 | ✅ 修复 | `/ready` 与 5xx details 不再返回客户端；新增回归测试 |

**结论**：Batch 1 代码级 + Docker 级验收门槛全部通过（除明确移至 Iteration 3 的 Outbox/Kafka 项）。.env 真实密码未进入 Git（gitignore 生效），`.env.example` 保留开发默认值并已标注"生产必须覆盖"。可以形成 `Batch 1 stabilized` 提交/tag。

## 7. Git 提交状态

- `1f5efd8` chore: stop tracking generated build artifacts（.gitignore 补充 + 1480 个 .next/coverage/tsbuildinfo 移出索引）
- `5f1e44d` Baseline after initial optimization batch（会话期间 git init 的初始提交，含 7 工作流改动）
- 工作区当前已含 Batch 1 全部修复、回归测试、findings 文档更新，已通过代码级 + Docker 级验收
- **.env 安全**：gitignore 生效，当前 `.env`（含随机强 JWT_SECRET / Keycloak / Grafana 密码）未追踪；`.env.example` 仅保留开发默认值并已标注生产覆盖
- env_vars.txt 未进入历史（已验证），GEMINI token 未泄露；crm_password 为 `.env.example` 既有开发默认值

### Batch 1 stabilized 提交命令
```bash
git add -A
git -c user.name=Dorring -c user.email=dorring@local commit -m "Batch 1 stabilized: P0 closed, single migration runner, real smoke test, secret hygiene, error sanitization, CI/RLS alignment"
git tag batch1-stabilized
```
执行后进入 Iteration 3（见第 8 节）。

## 8. Iteration 3 建议（Batch 1 完成后启动）

采用 **Gateway 内事务 Outbox**（不新建独立 Write API），以 **Lead 纵向切片**先行：
1. 命令契约 + CloudEvents schema
2. 单事务写业务状态 + Event Store + Outbox
3. 幂等键 + 乐观并发（expectedVersion）
4. Outbox Publisher 重试/DLQ + SKIP LOCKED 多实例
5. Kafka 停机故障注入
6. 重复/乱序/Replay 测试
7. 禁止 Lead 路由直接 publishEvent
Lead 全验收通过后推广到 Deal/Ticket/Customer/Approval/Automation。

Iteration 3 退出条件：
- DB 提交成功而 Kafka 停机时，事件不丢失（Outbox 积压可恢复）。
- Publisher 重试不会产生重复业务副作用（消费者幂等）。
- Replay 可全量恢复读模型。
- `expectedVersion` 能阻止并发覆盖。
- Outbox 最终清零或进入可观测失败状态。

## 9. Docker 环境验收命令清单（PowerShell / Windows）

在有 Docker Desktop（Compose v2）的 Windows 设备上，从仓库根目录用 PowerShell 依次执行。每步必须通过才进下一步。

> 已修正的矛盾：(1) migrate 服务现由 `database/Dockerfile.migrate`（node+prisma+psql）单一执行 **Prisma → SQL 01-11 → RLS 验证**，不再是只跑 SQL 的伪入口；(2) gateway/agents/migrate 的 `DATABASE_URL` 由 `POSTGRES_PASSWORD` 派生，不再有 `crm_password` vs `change-me-in-production` 错位；(3) Keycloak/Grafana 改用 `${VAR:?required}` 环境变量，不再硬编码 admin；(4) smoke-test 改跑 `scripts/smoke-test.sh`（注册→登录→建 Lead→列表→401），不再是健康检查占位；(5) Kafka 停机/Outbox 清零测试**移至 Iteration 3**（Lead 仍直接发 Kafka、Compose 未跑 Outbox Publisher，不冒充 Batch 1 能力）。

### 9.1 准备 .env
```powershell
Copy-Item .env.example .env
# 生成强 JWT_SECRET（>=32 字节），替换 .env 中已有的弱默认行
$jwt = -join ((48..122) | Get-Random -Count 48 | % {[char]$_})
$kc = -join ((48..122) | Get-Random -Count 24 | % {[char]$_})
$gf = -join ((48..122) | Get-Random -Count 24 | % {[char]$_})
$content = Get-Content .env
$content = $content -replace '^JWT_SECRET=.*', "JWT_SECRET=$jwt"
$content = $content -replace '^KEYCLOAK_ADMIN_PASSWORD=.*', "KEYCLOAK_ADMIN_PASSWORD=$kc"
$content = $content -replace '^GRAFANA_ADMIN_PASSWORD=.*', "GRAFANA_ADMIN_PASSWORD=$gf"
$content | Set-Content .env
# 确认无重复键、POSTGRES_PASSWORD 与 DATABASE_URL 一致（Compose 派生，无需手动对齐）
Get-Content .env | Select-String 'JWT_SECRET|KEYCLOAK_ADMIN_PASSWORD|GRAFANA_ADMIN_PASSWORD|POSTGRES_PASSWORD'
```

### 9.2 Compose 配置校验
```powershell
docker compose config | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: compose config"; exit 1 }
Write-Host "compose config OK"
# 校验无硬编码 supersecret / admin/admin：保存匹配结果，有匹配则失败
$leaked = docker compose config | Select-String 'supersecret|KEYCLOAK_ADMIN_PASSWORD=admin|GF_SECURITY_ADMIN_PASSWORD=admin'
if ($leaked) { Write-Host "FAIL: leaked default secrets:"; $leaked; exit 1 } else { Write-Host "OK: no leaked defaults" }
```

### 9.3 空库全量迁移 + 重复迁移（单一 runner：Prisma → SQL → RLS）
```powershell
# 只起 postgres
docker compose up -d postgres
# 等待 healthy
docker compose ps postgres
# 空库全量迁移：migrate 服务一次跑完 Prisma migrate deploy → SQL 01-11 → RLS 验证
docker compose --profile migrate run --rm migrate
# 期望输出：=== 1/3 Prisma === === 2/3 Raw SQL 01-11 === === 3/3 RLS verification ===
# 且 RLS 验证表 leads/customers/agent_decisions/data_retention_policies/outbox_events/event_streams 全部 rls_enabled=t rls_forced=t
# 重复迁移（幂等性，应退出 0）
docker compose --profile migrate run --rm migrate
if ($LASTEXITCODE -eq 0) { Write-Host "idempotent OK" } else { Write-Host "FAIL: re-run not idempotent"; exit 1 }
```

### 9.4 完整 stack + 真实 smoke test
```powershell
docker compose up -d
# 等待所有服务 healthy
docker compose ps   # gateway/agents/postgres/redis/kafka 应为 healthy
# 真实 smoke：注册→登录→建 Lead→列表→401 无 token（脚本内已含断言）
docker compose --profile smoke-test run --rm smoke-test
if ($LASTEXITCODE -eq 0) { Write-Host "smoke OK" } else { Write-Host "FAIL: smoke test"; exit 1 }
# 也可在主机直接跑 PowerShell 版
powershell -ExecutionPolicy Bypass -File scripts\smoke-test.ps1 -Gateway http://localhost:4000
```

### 9.5 Redis 停机认证（P0-1 fail-closed 验证）
必须用**有效 token**：invalid token 在 JWT 签名校验阶段就被拒（不经过 Redis 黑名单），无法证明 fail-closed。正确流程：注册+登录取有效 token → 停 Redis → 用有效 token 请求 → 期望 401（黑名单不可达 → fail-closed）。若返回 200 则 P0-1 修复失效（fail-open）。
```powershell
# 1. Redis 在线时注册+登录，取有效 token（register/login 是公开路由，不触达 Redis 黑名单）
$slug = "failclosed-$([int64](Get-Date -UFormat %s))"
$email = "$slug@example.com"
$body = @{ tenantName="FailClosed Co"; tenantSlug=$slug; name="FC User"; email=$email; password="SmokePass123!" } | ConvertTo-Json
Invoke-WebRequest -Uri http://localhost:4000/api/v1/auth/register -Method Post -ContentType "application/json" -Body $body -UseBasicParsing | Out-Null
$body = @{ tenantSlug=$slug; email=$email; password="SmokePass123!" } | ConvertTo-Json
$resp = Invoke-WebRequest -Uri http://localhost:4000/api/v1/auth/login -Method Post -ContentType "application/json" -Body $body -UseBasicParsing
$token = ($resp.Content | ConvertFrom-Json).accessToken
# 2. 停 Redis
docker compose stop redis
# 3. 用有效 token 请求 → 黑名单检查因 Redis 不可达 fail-closed → 期望 401
$code = (Invoke-WebRequest -Uri http://localhost:4000/api/v1/leads -Method Get -Headers @{ Authorization = "Bearer $token" } -SkipHttpErrorCheck).StatusCode
Write-Host "valid token + Redis down -> HTTP $code (expect 401, fail-closed)"
if ($code -ne 401) { Write-Host "FAIL: P0-1 regression — valid token accepted with Redis down (fail-open)"; exit 1 }
docker compose start redis
```

### 9.6 跨租户隔离 + Gateway 集成测试（专用 test 服务）
Gateway 最终镜像 `npm ci --omit=dev` 且不含测试源码，`docker compose exec gateway npm test` 无法运行 Jest。用 `test-gateway` 服务（builder 阶段：全 devDependencies + 测试源码 + Prisma client，`CRM_TEST_REQUIRE_DB=1`）跑完整 Jest 套件（含 RLS enforcement 的 5 个隔离向量）。
```powershell
# 跑完整 Gateway Jest 套件（21 nodb + 30 DB 集成，含 test_rls_enforcement）
docker compose --profile test run --rm test-gateway
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: gateway tests"; exit 1 }
# 若只跑 RLS 套件（覆盖跨租户 CRUD、Header 篡改、super_admin 读写、缓存隔离、WS 订阅）：
docker compose --profile test run --rm test-gateway sh -c "npm test -- --testPathPattern=test_rls_enforcement"
# Agents 镜像含 pytest（requirements.txt），可直接 exec
docker compose exec agents bash -c "CRM_TEST_REQUIRE_DB=1 pytest tests/test_tenant_isolation.py -v"
# OPA 策略测试
docker compose exec opa opa test /policies
```

### 9.7 移至 Iteration 3 的验收项（不作为 Batch 1 条件）
以下能力依赖 Outbox 主写链路，**当前不具备**，明确移至 Iteration 3 退出条件：
- Kafka 停机后 Outbox 积压清零（不丢事件）—— Lead 路由仍直接 `publishEvent()`，Compose 未跑 Outbox Publisher
- 重复命令幂等（同 idempotencyKey 返回原结果）—— 需 Outbox + Event Store
- 全量 Replay 后读模型与基准快照一致 —— 需 Projector + Replay service 接入拓扑
- expectedVersion 并发冲突 —— 需聚合乐观并发

### 9.8 通过判据
9.2~9.6 全部通过后形成提交 + tag：
```powershell
git add -A
git -c user.name=Dorring -c user.email=dorring@local commit -m "Batch 1 stabilized: P0 closed, single migration runner, real smoke test, secret hygiene"
git tag batch1-stabilized
```
此后方可进入 Iteration 3（见第 8 节），届时补 9.7 的 Outbox/Replay/幂等验收。
