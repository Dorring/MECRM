# M-Agent-ECRM 项目深度分析

> 本文档面向面试准备与源码学习，按模块逐文件展开，重点阐述**调用链路、前后端交互、事件流、治理机制**等关系。

---

## 一、系统概述

M-Agent-ECRM 是一个**企业级 AI 驱动的 CRM 平台**，核心能力包括：

- **CRM 基础**：线索、交易、工单、客户的全生命周期管理
- **AI 智能**：10+ 子 Agent 覆盖对话/搜索/自动化/旅程/知识/孪生/预测等
- **AI 治理**：急停开关、审批工作流、数据守护、可解释性
- **多租户隔离**：PostgreSQL RLS + OPA + 缓存隔离 全链路保障
- **CQRS + 事件溯源**：事件驱动架构，事务性发件箱保证最终一致性
- **弹性与灾备**：断路器、混沌工程、事件回放、完整灾备恢复

---

## 二、总体架构与调用关系

### 2.1 全局架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    Browser / Client                          │
└──────────────┬──────────────────────────────┬───────────────┘
               │ HTTP (REST API)               │ WebSocket (/ws)
┌──────────────▼──────────────────────────────▼───────────────┐
│                 Frontend (Next.js 16 :3000)                  │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐ │
│  │Dashboard │ │ChatPanel │ │KillSwitch│ │AutomationStudio│ │
│  └─────────┘ └──────────┘ └──────────┘ └────────────────┘ │
│  API Client: src/lib/api.ts → Gateway :4000                  │
│  WS Hook:   src/hooks/useWebSocket.tsx → /ws?token=JWT      │
└──────────────┬──────────────────────────────┬───────────────┘
               │ REST /api/v1/*                │ WS
┌──────────────▼──────────────────────────────▼───────────────┐
│              API Gateway (Express + TS :4000)                │
│                                                              │
│  ┌─── Middleware Chain ───────────────────────────────────┐  │
│  │ requestLogger → auth(JWT) → tenant → OPA → audit     │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  Routes (20 modules) → Prisma ORM → PostgreSQL              │
│  Routes → publishEvent() → Kafka Producer                   │
│                                                              │
│  Kafka Consumers (11) ← Kafka Topics → 更新读模型/缓存      │
│  WebSocket Server → sendToUser() / sendToTenant()           │
│  Secure Cache → Redis (租户隔离键)                           │
│  OPA Proxy → OPA :8181 (tenant_isolation + rbac + abac)     │
└──────┬───────────┬───────────────┬──────────┬───────────────┘
       │           │               │          │
┌──────▼──┐  ┌─────▼─────┐  ┌─────▼──────┐  │
│PostgreSQL│  │  Kafka    │  │   Redis    │  │
│:5432     │  │:9094      │  │:6379       │  │
│(RLS/CQRS)│  │(KRaft)    │  │(Cache/State)│  │
└──────────┘  └─────┬─────┘  └────────────┘  │
                    │                          │
┌───────────────────▼──────────────────────────▼──────────────┐
│           AI Agent Layer (Python :5010)                      │
│                                                              │
│  AgentOrchestrator → AgentRouter → Topic→Handler 映射        │
│                                                              │
│  ┌─ Domain Agents ──────────────────────────────────────┐   │
│  │ SalesAgent │ SupportAgent │ ComplianceAgent │         │   │
│  │ AnalyticsAgent                                        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─ Intelligence Agents ────────────────────────────────┐   │
│  │ ChatAgent (LangGraph+Ollama+Weaviate)                │   │
│  │ SearchAgent (意图→检索→排序→建议)                      │   │
│  │ AutomationAgent (规则解析→模拟→执行)                    │   │
│  │ JourneyAgent (阶段分类→时间线)                          │   │
│  │ ProductivityAgent (信号→建议→草稿)                     │   │
│  │ KnowledgeAgent (分类→摘要→发布)                        │   │
│  │ PredictiveAnalyticsAgent (特征→预测)                   │   │
│  │ ComplianceIntelligenceAgent (语义审计搜索)              │   │
│  │ DevXAgent / TwinsAgent / i18nAgent                    │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─ AI Governance (每个 Agent 执行前必经) ──────────────┐   │
│  │ KillSwitch(Redis) → Approval(Redis) → DataGuard(PG) │   │
│  │ → Explainability(PG) → OPA Policy                   │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  aiohttp HTTP API: /intelligence/query, /automation/parse    │
│  (Gateway 通过 X-Tenant-Id/X-User-Id 头转发调用)            │
└──────┬───────────┬───────────────┬──────────────────────────┘
       │           │               │
┌──────▼──────┐ ┌──▼────┐ ┌───────▼───────┐
│Core Services │ │  OPA  │ │  Weaviate     │
│(FastAPI)    │ │:8181  │ │:8082 (向量DB) │
│CQRS Write   │ │(Rego) │ │(语义搜索/记忆)│
│Outbox Pub   │ │       │ │               │
│DR/Governance│ │       │ │               │
└─────────────┘ └───────┘ └───────────────┘
```

### 2.2 核心请求流转

#### 前端 → Gateway → DB + Kafka（写操作，以创建线索为例）

```
1. 前端 leadsApi.create(data)
   → POST /api/v1/leads  (api.ts → fetch → Gateway)

2. Gateway 中间件链：
   requestLogger → auth(JWT验证+Redis黑名单) → tenant(提取tenantId) → OPA(3策略并行查询) → audit(记录)

3. leads.ts 路由处理器：
   → Prisma db.lead.create({data, tenantId})  写入 PostgreSQL
   → publishEvent(TOPICS.LEADS_CREATED, event) 发布到 Kafka crm.leads.created
   → publishEvent(TOPICS.LEADS_EVENTS, event)  发布到 Kafka crm.leads.events (事件溯源用)
   → res.status(201).json(lead) 返回前端

4. 前端收到 201 响应，TanStack Query 自动刷新列表
```

#### Kafka → Agent 编排（以线索创建事件为例）

```
1. Gateway 发布 crm.leads.created → Kafka

2. AgentOrchestrator (aiokafka Consumer) 消费消息
   → _process_message(): 先检查 KillSwitch
   → router.route("crm.leads.created", message)

3. AgentRouter._handle_lead_created():
   → productivity_signals_agent.ingest_event()  // 生产力信号摄入
   → data_guard.ensure_allowed()                 // 数据守护检查
   → guard.ensure_allowed() + sales_agent.qualify_lead()  // 治理+销售Agent线索评分
   → guard.ensure_allowed() + compliance_agent.validate_data()  // 治理+合规检查

4. SalesAgent.qualify_lead() 内部：
   → check_policy(OPA)  // 策略检查
   → call_llm(Ollama)   // LLM 推理
   → emit_event()       // 发布 crm.agents.action-proposed 到 Kafka
     (emit_event 内部: data_guard→guard→kafka→explainability)

5. Gateway Consumer 消费 Agent 产出事件，更新读模型
```

#### Agent → Gateway → DB（Chat Agent 调用 CRM 工具）

```
1. 前端 ChatPanel → POST /api/intelligence/query
   → Gateway intelligence 路由 → 代理到 Agent :5010

2. Agent ChatAgent.chat():
   → LangGraph Graph 执行
   → ToolExecutor 选择工具:
     - CrmReader: HTTP GET → Gateway /api/v1/leads 等 (带 Authorization 头)
     - CrmWriter: HTTP POST → Gateway /api/v1/xxx (写 CRM 数据)
     - VectorSearch: → Weaviate 语义搜索
     - SearchAdapter: → SearchAgent 搜索

3. ChatAgent 返回 {conversation_id, message, suggested_replies, action_proposals}
   → Gateway 转发响应 → 前端
```

---

## 三、技术栈详解（面试重点）

### 3.1 前端技术栈

| 技术 | 版本 | 作用 | 面试亮点 |
|------|------|------|----------|
| **Next.js** | 16 | React 全栈框架，App Router | SSR/SSG、Server Components、API 代理 |
| **React** | 18 | UI 渲染 | 并发特性、Suspense、Transitions |
| **Tailwind CSS** | 3 | 原子化 CSS | JIT 编译、Design Token |
| **Zustand** | 4 | 客户端全局状态 | 极轻量（~1KB）、无 Provider、selector 优化 |
| **TanStack Query** | 5 | 服务端状态管理 | 自动缓存/失效、乐观更新、无限滚动 |
| **React Hook Form + Zod** | 7+3 | 表单+校验 | 非受控组件性能、Schema 复用前后端 |
| **Recharts** | 2 | 数据可视化 | 声明式图表、组合式 API |
| **Socket.IO Client** | 4 | WebSocket 实时通信 | 双向推送（审批通知、Agent 建议等） |

### 3.2 API Gateway 技术栈

| 技术 | 版本 | 作用 | 面试亮点 |
|------|------|------|----------|
| **Express** | 4 | HTTP 服务器 | 中间件洋葱模型、路由模块化 |
| **TypeScript** | 5.3 | 类型安全 | strict 模式、泛型中间件 |
| **Prisma** | 5 | PostgreSQL ORM | 类型安全查询、迁移管理、多租户 withTenantDb |
| **KafkaJS** | 2 | Kafka 客户端 | 生产者/消费者、事务、DLQ |
| **ioredis** | 5 | Redis 客户端 | Pipeline、Lua 脚本、集群 |
| **ws** | 8 | WebSocket 服务端 | 心跳、租户隔离订阅、JWT 认证 |
| **Helmet** | 7 | 安全 HTTP 头 | CSP、CORS、HSTS 等 |
| **jsonwebtoken** | 9 | JWT | RS256/HS256、Refresh Token、黑名单 |
| **prom-client** | 15 | Prometheus 指标 | 直方图、计数器、默认+自定义指标 |
| **OpenTelemetry** | - | 分布式追踪 | 跨服务 trace、span propagation |

### 3.3 AI Agent 层技术栈

| 技术 | 版本 | 作用 | 面试亮点 |
|------|------|------|----------|
| **LangGraph** | 0.0.50+ | Agent 编排图 | 状态图、条件边、检查点、人机协同 |
| **LangChain** | 0.1+ | LLM 框架 | Prompt 模板、输出解析、工具绑定 |
| **Ollama** | - | 本地 LLM 推理 | llama3.1、私有化部署、无 API 费用 |
| **aiokafka** | 0.10+ | 异步 Kafka | 高吞吐消费者、手动提交 offset |
| **asyncpg** | 0.29+ | 异步 PostgreSQL | 连接池、事务、性能远超 psycopg2 |
| **Weaviate** | 1.23 | 向量数据库 | 语义搜索、嵌入存储、Chat 记忆 |
| **FastAPI** | 0.110+ | CQRS 写端 API | 异步、依赖注入、自动 OpenAPI |
| **structlog** | 23+ | 结构化日志 | JSON 输出、上下文绑定、级别过滤 |
| **Pydantic** | 2 | 数据校验 | v2 性能提升、Schema 生成 |

### 3.4 数据层

| 技术 | 作用 | 面试亮点 |
|------|------|----------|
| **PostgreSQL 16** | 主数据库 | RLS 行级安全（多租户核心）、CQRS 读写分离、事务性发件箱 |
| **Redis 7** | 缓存+状态 | 安全缓存（租户隔离键）、Kill Switch 状态、Token 黑名单、PubSub |
| **Kafka 7.5 KRaft** | 事件流 | 无 ZooKeeper、20+ Topic、分区按 tenantId 路由、幂等消费 |
| **Weaviate** | 向量搜索 | 语义搜索 CRM 数据、Chat Agent 长期记忆、Ollama 嵌入 |

### 3.5 认证授权

| 技术 | 作用 | 面试亮点 |
|------|------|----------|
| **Keycloak 23** | 身份提供商 | OAuth2/OIDC、SSO、用户管理 |
| **JWT** | 令牌认证 | Access Token + Refresh Token、Redis 黑名单 |
| **OPA + Rego** | 策略引擎 | RBAC+ABAC+租户隔离 3 策略并行查询、结果缓存、Fail-Closed |

---

## 四、逐文件详细分析

### 4.1 前端 `frontend/`

#### 4.1.1 配置层

| 文件 | 详解 |
|------|------|
| `package.json` | 依赖声明。核心：next@16、react@18、zustand@4、@tanstack/react-query@5、react-hook-form@7、zod@3、recharts@2、socket.io-client@4。脚本：`dev`(Turbopack)、`build`、`lint` |
| `next.config.js` | Turbopack 启用、standalone 输出模式（Docker 部署优化）、API 代理 rewrite：`/api/*` → `NEXT_PUBLIC_API_URL`（Gateway）、路径别名 `@` → `src/` |
| `tsconfig.json` | ES2017 目标、ESNext 模块、react-jsx JSX 转换、路径别名 `@/*` → `./src/*` |
| `.eslintrc.json` | 继承 next/core-web-vitals 规则集 |

#### 4.1.2 API 客户端 `src/lib/api.ts`（**前后端交互核心**）

```
ApiClient 类:
├── request<T>(endpoint, options)    // 统一请求方法
│   ├── normalizeEndpoint()          // 自动加 /api/v1 前缀
│   ├── AbortController + timeout    // 10s 超时控制
│   ├── localStorage.getItem('accessToken') // 从 localStorage 读 JWT
│   ├── Authorization: Bearer <token> // 注入请求头
│   └── fetch() → response.json()    // 原生 fetch，非 Axios
├── get/post/put/patch/delete        // REST 方法封装
└── 导出具体 API:
    ├── leadsApi = createServiceApi('/api/v1/leads')  // 通用 CRUD
    ├── dealsApi                                     // + updateStage
    ├── ticketsApi                                   // + resolve
    ├── customersApi                                 // + timeline, profile
    ├── approvalsApi                                 // + decide
    ├── productivityApi                              // listProposals, decide
    ├── automationsApi                               // parse/create/simulate/activate/pause/resume
    ├── predictionsApi                               // latest(entityType, entityIds)
    ├── governanceApi                                // killSwitchStatus, decisions, pauseTenantAgents, emergencyStop
    ├── auditApi                                     // search, policies
    ├── knowledgeApi                                 // drafts/articles CRUD + approve/reject
    └── replayApi                                    // start/status/timeline/diff
```

**前后端接口对应关系**：

| 前端 API 方法 | Gateway 路由 | 说明 |
|----------------|-------------|------|
| `leadsApi.create(data)` | `POST /api/v1/leads` | 创建线索 → 发布 `crm.leads.created` |
| `leadsApi.list({status})` | `GET /api/v1/leads?status=` | 列表查询（Prisma findMany） |
| `dealsApi.updateStage(id, stage)` | `PUT /api/v1/deals/:id/stage` | 更新交易阶段 → 发布 `crm.deals.stage-changed` |
| `ticketsApi.resolve(id, resolution)` | `POST /api/v1/tickets/:id/resolve` | 解决工单 → 发布 `crm.tickets.resolved` |
| `customersApi.timeline(id)` | `GET /api/v1/customers/:id/timeline` | 客户事件时间线 |
| `approvalsApi.decide(id, decision)` | `POST /api/v1/approvals/:id/decide` | 审批决策 → 发布 `crm.approvals.decision` |
| `automationsApi.parse(nlRuleText)` | `POST /api/v1/automations/parse` | 自然语言规则解析（→ Agent） |
| `automationsApi.simulate(id)` | `POST /api/v1/automations/:id/simulate` | 自动化模拟 → 发布 `crm.automation.simulation.requested` |
| `governanceApi.emergencyStop()` | `POST /api/v1/governance/killswitch/emergency-stop` | 紧急停止 AI |
| `governanceApi.killSwitchStatus()` | `GET /api/v1/governance/killswitch/status` | 急停状态查询 |
| `productivityApi.listProposals()` | `GET /api/v1/productivity/proposals` | 生产力建议列表 |
| `knowledgeApi.approveDraft(id)` | `POST /api/v1/knowledge/drafts/:id/approve` | 审批知识草稿 |
| `auditApi.search(data)` | `POST /api/v1/audit/search` | 语义审计搜索（→ Agent） |
| `replayApi.start(data)` | `POST /api/v1/replay/jobs` | 启动事件回放 |

#### 4.1.3 页面层 `src/app/`

| 页面路径 | 组件 | 数据来源 | 说明 |
|----------|------|----------|------|
| `/` (page.tsx) | Dashboard | leadsApi.list, dealsApi.list, ticketsApi.list | 聚合仪表盘 |
| `/leads` | LeadFormModal + 列表 | leadsApi | 线索管理 CRUD |
| `/deals` | 列表 + RiskBadges | dealsApi, predictionsApi | 交易+风险预测 |
| `/tickets` | 列表 | ticketsApi | 工单管理 |
| `/customers`, `/customers/[id]` | CustomerTimeline, CustomerTwin | customersApi | 客户详情+数字孪生 |
| `/agents` | KillSwitch, ExplainabilityPanel | governanceApi | AI Agent 控制台 |
| `/approvals` | ActionInbox | approvalsApi | 审批队列 |
| `/automations` | AutomationStudio | automationsApi | 自然语言→规则→模拟→激活 |
| `/governance` | KillSwitch, ExplainabilityPanel | governanceApi | AI 治理仪表盘 |
| `/knowledge`, `/knowledge/articles`, `/knowledge/review` | KnowledgeBase, KnowledgeReview | knowledgeApi | 知识库 CRUD+审核 |
| `/productivity` | ActionInbox | productivityApi | AI 生产力建议 |
| `/replay` | ReplayControls, EventTimeline | replayApi | 事件回放控制台 |

#### 4.1.4 组件层 `src/components/`

| 组件 | 功能 | 与后端交互 |
|------|------|------------|
| `ChatPanel.tsx` | AI 对话面板，支持多轮对话 | → `/api/intelligence/query` (WebSocket 或 HTTP) |
| `CommandBar.tsx` | 快捷命令栏，模糊搜索 | → intelligence API |
| `KillSwitch.tsx` | 急停开关 UI，可全局/单租户/单 Agent | → governanceApi.emergencyStop / pauseTenantAgents |
| `AutomationStudio.tsx` | 自然语言输入 → 规则预览 → 模拟 → 激活 | → automationsApi (parse/simulate/activate) |
| `ExplainabilityPanel.tsx` | 显示 AI 决策的理由、置信度、证据 | → governanceApi.decisions |
| `CustomerTwin.tsx` | 数字孪生视图，行为模拟 | → `/api/intelligence/twin/*` |
| `SimulationReport.tsx` | 自动化模拟结果展示 | 由 AutomationStudio 传入 |
| `VoiceButton.tsx` | 语音输入按钮 | → useVoiceInput Hook → `/api/intelligence/voice` |
| `EventTimeline.tsx` | 事件溯源时间线 | → replayApi.timeline |
| `TelemetryProvider.tsx` | OpenTelemetry 前端 Provider | 前端 trace/span 上报 |

#### 4.1.5 Hooks

| Hook | 功能 |
|------|------|
| `useWebSocket.tsx` | 建立 WS 连接 `/ws?token=JWT`，自动重连，消息分发。用于实时接收审批通知、AI 建议等 |
| `useVoiceInput.ts` | 调用 Web Speech API 或后端 `/api/intelligence/voice` 进行语音转文字 |

---

### 4.2 API Gateway `gateway/`

#### 4.2.1 入口 `src/index.ts`（**Gateway 启动核心**）

```
启动流程:
1. Express app 初始化
2. 全局中间件挂载:
   helmet(安全头) → cors → compression → rateLimit → json解析 → correlationId → requestLogger
3. setupMetrics(app)        // /metrics 端点
4. /health, /ready 端点     // 健康检查（检查 PG/Redis/Kafka）
5. 公开路由: /api/v1/auth (限流 30次/15min)
6. 受保护路由组 /api/v1:
   authMiddleware → tenantMiddleware → opaMiddleware → auditMiddleware
   → 挂载 17 个路由模块
7. 特殊路由 (独立中间件栈):
   /api/intelligence, /api/productivity, /api/intelligence/voice,
   /api/intelligence/twin, /api/intelligence/devx
8. WebSocket: new WebSocketServer({server, path: '/ws'}) → setupWebSocket()
9. Kafka Consumers 启动 (11 个, 可通过环境变量禁用)
10. server.listen(4000)
11. 优雅关闭: SIGTERM/SIGINT → 关闭 WS → 停止 Consumers → 断开 Kafka → 关闭 HTTP
```

#### 4.2.2 中间件链（**请求处理核心流程**）

**`middleware/auth.ts`** — JWT 认证
```
流程:
1. 从 Authorization 头提取 Bearer Token
2. Redis 查询 blacklist:<token> → 若存在则拒绝（已登出）
3. jwt.verify(token, JWT_SECRET) 解码
4. 提取 sub(userId)、tenantId、roles
5. 注入 req.user、req.tenantId
6. 设置下游头: x-user-id, x-token-tenant-id, x-user-roles
```

**`middleware/tenant.ts`** — 租户隔离
```
流程:
1. 从 req.user.tenantId 取 Token 中的租户
2. 若 x-tenant-id 头与 Token 不同 → 检查是否 super_admin
3. super_admin 可跨租户读，但 OPA 层限制写
4. 设置 req.tenantId 和 x-tenant-id 头（供下游服务使用）
```

**`middleware/opa.ts`** — OPA 策略执行（**核心安全层**）
```
流程:
1. 构建 OPA Input: {tenant_id, user, action, resource, actor_type}
2. GET 请求: 先查安全缓存 (Redis, 租户隔离键, epoch 版本化)
   → 缓存命中: 直接返回 allow/deny
   → 缓存未命中: 查询 OPA
3. 并行查询 3 个 OPA 策略:
   - enterprise_crm/tenant_isolation → 租户隔离
   - enterprise_crm/rbac → 基于角色
   - enterprise_crm/abac → 基于属性
4. 合并结果: all policies must allow, deny 消息合并
5. 若 result.requires_approval → 设置 x-requires-approval 头
6. 写入安全缓存 (TTL 30s)
7. OPA 不可用时: FAIL CLOSED (拒绝请求)
```

**`middleware/audit.ts`** — 审计日志
```
流程:
1. 记录请求: userId, tenantId, method, path, statusCode, duration
2. 写入 AuditLog (Prisma) 或发布到 crm.audit.events Kafka
```

#### 4.2.3 Kafka 服务 `services/kafka.ts`（**事件发布核心**）

```
DomainEvent 接口 (CloudEvents 格式):
  specversion, type, source, id, time, datacontenttype, tenantid, correlationid, data

publishEvent(topic, event):
  1. 补全 CloudEvents 标准字段 (specversion, time, datacontenttype)
  2. kafkaProducer.send({topic, messages: [{key: tenantid, value, headers}]})
  3. headers 包含 ce-type, ce-source, ce-id, ce-tenantid (CloudEvents 规范)
  4. kafkaMessagesPublished 指标 +1

TOPICS 常量: 40+ 个 Kafka Topic 名称定义
  crm.leads.* / crm.deals.* / crm.tickets.* / crm.approvals.* /
  crm.productivity.* / crm.automation.* / crm.intelligence.* / crm.journey.* / crm.analytics.*
```

#### 4.2.4 WebSocket 服务 `services/websocket.ts`

```
setupWebSocket(wss):
  1. 连接认证: 从 URL query 提取 token → jwt.verify → 提取 userId/tenantId
  2. 连接管理: Map<tenantId, Map<userId, Set<ws>>> 三级映射
  3. 心跳: 30s interval ping/pong，超时 terminate
  4. 订阅: subscribe 消息 → 验证 topic 前缀 tenant:<tenantId>: → 拒绝跨租户订阅
  5. 推送 API:
     - sendToUser(tenantId, userId, msg)   // 推送给特定用户
     - sendToTenant(tenantId, msg)         // 推送给租户所有用户
     - broadcast(msg)                      // 全局广播
```

#### 4.2.5 路由模块（以 `routes/leads.ts` 为例）

```
创建线索 POST /:
  1. express-validator 校验 body
  2. Prisma db.lead.create({data: {tenantId: req.tenantId, ...}})
  3. publishEvent(TOPICS.LEADS_CREATED, {type, source, id, tenantid, data})
  4. publishEvent(TOPICS.LEADS_EVENTS, {..., aggregate_type, aggregate_id}) // 事件溯源
  5. res.status(201).json(lead)

列表 GET /:
  1. Prisma db.lead.findMany({where: {tenantId: req.tenantId, ...}, skip, take, orderBy})
  2. res.json({data, pagination})

所有路由共同模式:
  - 读取: Prisma findMany/findFirst + tenantId 过滤
  - 写入: Prisma create/update + publishEvent 到 Kafka
  - 删除: Prisma delete + tenantId 校验
```

#### 4.2.6 Kafka 消费者（以 `consumers/approvalsRequired.ts` 为例）

```
startApprovalsRequiredIngestor():
  1. createConsumer('gateway-approvals-required')
  2. 订阅 TOPICS.APPROVALS_REQUIRED = 'crm.approvals.required'
  3. 收到事件 → 解析 JSON → 提取 tenantId, approvalId, actionType 等
  4. withTenantDb(tenantId) → db.approval.create({id, tenantId, status:'pending', ...})
  5. 即: Agent 请求审批 → Kafka → Gateway Consumer → 写入 Approval 表 → 前端轮询/WS推送

11 个消费者对应关系:
  crm.approvals.required          → 写入 Approval 表
  crm.audit.events                → 写入 AuditLog 表
  crm.productivity.action-suggested → 写入 ProductivityProposal 表
  crm.journey.updated             → 更新 CustomerTimeline
  crm.analytics.prediction-generated → 写入 Prediction 表
  crm.automation.simulation.result  → 写入 AutomationSimulation 表
  crm.automation.executed          → 更新 AutomationExecution
  crm.automation.action.requested  → 执行自动化动作
  crm.automation.activation.decision → 更新 AutomationPolicy 状态
  crm.knowledge.draft.created      → 通知知识审核
  crm.cache.invalidation           → 清除 Redis 缓存
```

---

### 4.3 AI Agent 层 `agents/`

#### 4.3.1 入口 `src/orchestrator/main.py`

```
main() 启动流程:
1. run_health_server()  // aiohttp HTTP 服务 :5010
   - 注册路由:
     GET  /health
     GET  /metrics
     POST /api/v1/intelligence/query    → intelligence_query_handler
     POST /api/v1/intelligence/voice    → voice_handler
     POST /api/v1/intelligence/voice/query → voice_query_handler
     POST /api/v1/automation/parse      → automation_parse_handler
     POST /api/v1/audit/search          → audit_search_handler
   - 初始化 Agent 实例:
     SearchAgent, ChatAgent(ToolExecutor(CrmReader,CrmWriter,VectorSearch,SearchAdapter)),
     AutomationAgent, AuditIndexer, ComplianceIntelligenceAgent
   - _startup: 启动 SearchAgent, ChatAgent, AuditIndexer
   - _cleanup: 关闭资源

2. AgentOrchestrator.start()
   - AIOKafkaConsumer 订阅 21 个 Topic (settings.CONSUME_TOPICS)
   - AIOKafkaProducer 初始化
   - router.initialize(producer)  // 初始化所有 Agent + 治理组件
   - _consume_loop()  // 持续消费 Kafka 消息
```

**HTTP Handler 与前端关系**（Gateway 代理转发）：

| Agent HTTP 端点 | Gateway 代理路由 | 前端触发 |
|-----------------|-----------------|----------|
| `POST /api/v1/intelligence/query` | `/api/intelligence` | ChatPanel 对话、CommandBar 搜索 |
| `POST /api/v1/intelligence/voice` | `/api/intelligence/voice` | VoiceButton 语音输入 |
| `POST /api/v1/automation/parse` | `/api/v1/automations/parse` | AutomationStudio 规则解析 |
| `POST /api/v1/audit/search` | `/api/v1/audit/search` | AuditSearch 语义搜索 |

#### 4.3.2 路由器 `src/orchestrator/router.py`（**事件分派核心**）

```
AgentRouter.__init__():
  实例化 11 个 Agent + 4 个治理组件:
  - SalesAgent, SupportAgent, ComplianceAgent, AnalyticsAgent
  - ProductivitySignalsAgent, ProductivityAgent
  - JourneyAgent, PredictiveAnalyticsAgent
  - AutomationSimulationAgent, AutomationExecutorAgent
  - KnowledgeAgent
  - AgentKillSwitch(Redis), GovernanceGuard(KillSwitch)
  - ApprovalService(Redis), ExplainabilityEngine(PG), DataGuard(PG)

  Topic → Handler 映射 (21 条):
  crm.leads.created        → _handle_lead_created
  crm.leads.updated        → _handle_lead_updated
  crm.deals.created        → _handle_deal_created
  crm.deals.stage-changed  → _handle_deal_stage_changed
  crm.tickets.created      → _handle_ticket_created
  crm.tickets.updated      → _handle_ticket_updated
  crm.tickets.resolved     → _handle_ticket_resolved
  crm.deals.updated/closed → _handle_productivity_and_journey_source_event
  crm.productivity.signal  → _handle_productivity_signal
  crm.journey.updated      → _handle_journey_updated → PredictiveAnalyticsAgent
  crm.approvals.decision   → _handle_approval_decision
  crm.automation.simulation.requested → _handle_automation_simulation_requested
  ...
```

**事件处理链路详解**（以线索创建为例）：

```
_handle_lead_created(event):
  1. 提取 tenant_id，缺失则跳过
  2. productivity_signals_agent.ingest_event()  // 信号摄入
  3. data_guard.ensure_allowed()                 // PII/数据守护检查
  4. guard.ensure_allowed() → sales_agent.qualify_lead(event)
     // KillSwitch 检查 → SalesAgent 线索评分
  5. guard.ensure_allowed() → compliance_agent.validate_data(event, "lead")
     // KillSwitch 检查 → ComplianceAgent 数据合规验证

治理检查模式 (每个 Agent 调用前):
  if await _blocked_data(data_guard, tenant_id, agent_id, event): return  # 数据守护
  if await _blocked(guard, tenant_id, agent_id): return                  # KillSwitch
  await agent.xxx(event)                                                  # 执行
```

#### 4.3.3 BaseAgent `src/agents/base.py`（**所有 Agent 的基类**）

```
BaseAgent 核心方法:

check_policy(tenant_id, action, resource, confidence):
  1. 构建 policy_input: {agent, tenant_id, action, context, resource, confidence}
  2. 并行查询 OPA:
     - POST OPA/v1/data/enterprise_crm/agents → core 策略
     - POST OPA/v1/data/enterprise_crm/agents/approval → 审批策略
  3. 返回 {allowed, requires_approval, deny_reasons, approvers, priority}
  4. OPA 不可用时: FAIL CLOSED (allowed=False)

emit_event(topic, event_type, tenant_id, data):
  1. data_guard.ensure_allowed()  // 数据守护: 检查 PII/数据权限
  2. guard.ensure_allowed()       // KillSwitch: 检查 Agent 是否被暂停
  3. 构建 CloudEvents 格式事件
  4. producer.send(topic, value, key=tenant_id)  // 发送到 Kafka
  5. explainability.record_decision()  // 记录决策到 PG (可解释性)

request_approval(tenant_id, action_type, ...):
  1. data_guard + guard 检查
  2. emit_event("crm.approvals.required", ...)  // 发布审批请求到 Kafka
  // → Gateway Consumer 写入 Approval 表 → 前端审批队列显示

call_llm(prompt, system_prompt):
  1. guard.ensure_allowed()  // KillSwitch 检查
  2. POST OLLAMA_URL/api/chat  // 调用 Ollama 本地 LLM
  3. 返回 LLM 响应文本

emit_reasoning(tenant_id, task_id, reasoning, confidence, factors):
  → emit_event("crm.agents.reasoning", ...)  // 发布推理过程（透明性）
```

#### 4.3.4 治理组件 `src/governance/`

**`kill_switch.py`** — 急停开关
```
AgentKillSwitch(Redis):
  - 状态存储: Redis Hash governance:killswitch:<scope_key>
  - 三种状态: RUNNING / PAUSED / KILLED
  - scope_key: "global" 或 "tenant:<tenantId>" 或 "agent:<agentId>"
  - decision(tenant_id, agent_id):
    1. 查询 global → tenant → agent 三级状态
    2. 任一级 PAUSED/KILLED → blocked=True
  - Redis PubSub 监听状态变更事件
```

**`approval_service.py`** — 审批工作流
```
ApprovalService(Redis):
  - pending 动作存储: Redis String approval:pending:<approvalId>
  - request_approval(): 保存 PendingAction 到 Redis
  - pop_pending(approvalId): 审批决策后取出待执行动作
  - 流程: Agent → request_approval() → emit_event(crm.approvals.required)
    → Kafka → Gateway Consumer 写 Approval 表
    → 前端审批 → Gateway POST /approvals/:id/decide
    → emit_event(crm.approvals.decision) → Kafka
    → Agent Router._handle_approval_decision() → pop_pending() → 执行
```

**`data_guard.py`** — 数据守护
```
DataGuard(PostgreSQL):
  - ensure_allowed(tenant_id, agent_id, customer_id, user_id):
    1. 检查 GDPR 擦除记录: 若用户已被遗忘 → 阻止
    2. 检查 PII 注册表: 标记敏感字段
    3. 检查数据保留策略: 是否已过期
  - 防止 AI 访问已擦除/过期的用户数据
```

**`explainability.py`** — 可解释性
```
ExplainabilityEngine(PostgreSQL):
  - record_decision(DecisionArtifact):
    写入 agent_decisions 表:
    {id, tenant_id, agent_id, action_type, risk_level, status,
     confidence, input_context, reasoning, evidence, tool_calls, approval_id}
  - 前端 ExplainabilityPanel 展示决策理由
```

**`guard.py`** — 治理守卫（统一入口）
```
GovernanceGuard(KillSwitch):
  ensure_allowed(tenant_id, agent_id):
    → kill_switch.decision()
    → 若 blocked → raise GovernanceBlocked
```

#### 4.3.5 Intelligence Agent 详解

**ChatAgent** (`intelligence/chat/`)
```
ChatAgent.__init__():
  - llm = ChatOllama(base_url, model="llama3.1")
  - memory = WeaviateChatMemory(weaviate_url, ollama_url)  // Weaviate 存对话记忆
  - deps = ChatDeps(llm, tool_executor, memory, memory_window=12)
  - graph = build_chat_graph(deps)  // LangGraph 状态图

chat(tenant_id, user_id, roles, query, conversation_id):
  1. emit_event("crm.intelligence.user-query")  // 发布查询事件
  2. LangGraph graph 执行:
     a. 意图分类 (ChatIntent)
     b. 若需工具调用 → ToolExecutor:
        - CrmReader:  HTTP GET → Gateway /api/v1/leads/:id 等
        - CrmWriter:  HTTP POST → Gateway /api/v1/xxx
        - VectorSearch: → Weaviate 语义搜索
        - SearchAdapter: → SearchAgent.search()
     c. 生成响应 + suggested_replies + action_proposals
  3. 返回 ChatResponse
```

**SearchAgent** (`intelligence/search/`)
```
search(tenant_id, user_id, roles, query, module):
  1. intent_parser.parse(query)  // 意图解析
  2. retriever.retrieve(query, filters)  // Weaviate 检索
  3. ranker.rank(results, query)  // 排序
  4. suggestions.generate(results)  // 建议生成
  5. 发布 crm.intelligence.search-performed 事件
```

**AutomationAgent** (`intelligence/automation/`)
```
parse(tenant_id, user_id, roles, nl_rule_text):
  1. LLM 解析自然语言 → 结构化规则 (trigger, conditions, actions)
  2. workflow_compiler.compile(rules) → 可执行工作流
  3. 返回解析结果 + 预览

simulate(policy, fromTs, toTs):
  1. 从事件存储加载历史事件
  2. simulator.run(rules, events) → 模拟执行
  3. 返回模拟报告 (匹配次数, 影响范围, 风险评估)

execute(trigger_event):
  1. 匹配触发规则
  2. 若需审批 → request_approval()
  3. executor.run(actions) → 执行动作
  4. emit_event("crm.automation.executed")
```

**JourneyAgent** (`intelligence/journey/`)
```
process(event):
  1. stage_classifier.classify(event) → 客户所处阶段
  2. timeline_builder.update(customer_id, stage, event)
  3. emit_event("crm.journey.updated") → 触发 PredictiveAnalyticsAgent
```

**ProductivityAgent** (`intelligence/productivity/`)
```
ProductivitySignalsAgent.ingest_event(topic, event):
  信号检测: 检测 CRM 事件中的生产力信号
  → emit_event("crm.productivity.signal")

ProductivityAgent.handle_signal(signal):
  1. 信号分析 → 生成建议 (draft_generator)
  2. proposals.create() → 创建生产力建议
  3. 若需审批 → request_approval()
  4. emit_event("crm.productivity.action-suggested")
```

**KnowledgeAgent** (`intelligence/knowledge/`)
```
handle_ticket_resolved(evt) / handle_conversation_closed(evt):
  1. 从解决工单/关闭对话中提取知识
  2. classifier.classify(content) → 分类
  3. summarizer.summarize(content) → 摘要
  4. 创建知识草稿 → emit_event("crm.knowledge.draft.created")
  // → Gateway Consumer → 前端知识审核
```

**PredictiveAnalyticsAgent** (`intelligence/analytics/`)
```
process(journey_updated_event):
  1. feature_extraction.extract(customer_data) → 特征向量
  2. predictors.predict(features) → 预测结果 (流失概率, 升级概率等)
  3. emit_event("crm.analytics.prediction-generated")
  // → Gateway Consumer → 写入 Prediction 表 → 前端显示 RiskBadges
```

---

### 4.4 Core Services `core_services/`

#### 4.4.1 CQRS 写端 API `src/api.py`

```
FastAPI 应用:
  POST /commands/leads  → command_create_lead()
    1. Header X-Tenant-Id 提取租户
    2. tenant_transaction(pool, tenant_id)  // 设置 RLS 上下文
    3. create_lead(conn, store, outbox, cmd)
       → EventStore.append()  // 追加事件到 event_store 表
       → Outbox.insert()      // 在同一事务中写入 outbox_events 表
    4. 返回 {aggregate_id, version}
```

#### 4.4.2 事件存储 `src/write/event_store.py`

```
EventStore:
  - append(conn, aggregate_type, aggregate_id, event_type, data, tenant_id):
    INSERT INTO event_store (aggregate_type, aggregate_id, version, event_type, data, tenant_id)
    // version 自增，保证顺序
```

#### 4.4.3 事务性发件箱 `src/write/outbox.py`

```
TransactionalOutbox:
  - insert(conn, event_id, topic, payload, tenant_id):
    INSERT INTO outbox_events (event_id, topic, payload, tenant_id)
    // 在同一 PG 事务中，保证事件写入和发件箱记录的原子性
```

#### 4.4.4 发件箱发布器 `services/outbox_publisher.py`

```
main() 循环:
  1. asyncpg 连接池
  2. AIOKafkaProducer 初始化
  3. 获取活跃租户列表
  4. while True:
     for tenant_id in tenants:
       _process_tenant():
         1. SET app.tenant_id  // RLS 上下文
         2. SELECT ... FROM outbox_events WHERE published_at IS NULL
            FOR UPDATE SKIP LOCKED LIMIT batch_size  // 无锁读取
         3. 对每条记录:
            a. CircuitBreaker.call(producer.send_and_wait(topic, value, key))
            b. 成功 → UPDATE published_at = now()
            c. 失败 → 重试 (指数退避) 或 dead_letter (超过 max_retries)
     5. sleep(poll_interval)

关键设计:
  - FOR UPDATE SKIP LOCKED: 多实例并发安全，无阻塞
  - CircuitBreaker: Kafka 不可用时自动熔断
  - 指数退避 + Jitter: 避免重试风暴
  - Dead Letter: 超过重试上限标记为 dead_lettered_at
```

#### 4.4.5 安全缓存 `src/cache/secure_cache.py`

```
SecureCache:
  - buildKey({tenantId, policyId, epochs, userId, rolesHash, policyHash, resource}):
    // 租户隔离的缓存键: tenant:<tid>:policy:<pid>:epoch:<e>:user:<uid>:...
  - getEpochs(tenantId, policyId, userId):
    // 版本化 epoch: 策略变更时递增 epoch，使旧缓存失效
  - 确保缓存不会跨租户泄漏
```

#### 4.4.6 数据治理 `src/governance/`

| 文件 | 核心方法 | 调用者 |
|------|----------|--------|
| `data_erasure.py` | `forget_user(tenant_id, user_id)` 级联擦除所有 PII | GDPR API / tests |
| `data_export.py` | `export_user_data(tenant_id, user_id)` 导出用户全部数据 | GDPR API / tests |
| `pii_registry.py` | `mark_pii(table, column)` / `check_pii(data)` | DataGuard |
| `retention_policy.py` | `apply_retention(policy_id)` 按策略清理过期数据 | scripts/apply_retention_policies.py |

#### 4.4.7 弹性组件 `src/resilience/`

| 文件 | 核心方法 |
|------|----------|
| `circuit_breaker.py` | `call(fn)` → 三态 (Closed/Open/HalfOpen)，自动熔断和恢复 |
| `retry_policy.py` | `retry_async(fn, policy)` → 指数退避 + Jitter + 超时 |

---

### 4.5 数据库 `database/`

| 迁移 | 表 | 面试重点 |
|------|-----|----------|
| `01-core-tables.sql` | tenants, users, roles, user_roles, policies, leads, deals, tickets, customers | 基础实体，所有表含 tenant_id 列 |
| `02-rls-policies.sql` | RLS Policy | **核心**: `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `CREATE POLICY tenant_isolation ON ... USING (tenant_id = current_setting('app.tenant_id')::uuid)` |
| `03-event-log.sql` | domain_events | 事件日志，事件溯源基础 |
| `04-aggregate-snapshots.sql` | aggregate_snapshots | 快照优化回放性能 |
| `05-replay-jobs.sql` | replay_jobs | 管理回放任务进度 |
| `06-event-store.sql` | event_store | CQRS 写端事件存储 (aggregate_type, aggregate_id, version, event_type, data) |
| `07-outbox.sql` | outbox_events | 事务性发件箱 (published_at, retry_count, next_attempt_at, dead_lettered_at) |
| `08-read-models.sql` | read_models | CQRS 读端优化查询 |
| `09-agent-decisions.sql` | agent_decisions | AI 决策记录 (可解释性) |
| `10-data-governance.sql` | pii_fields, gdpr_erasure_requests, data_retention_policies | 数据治理 |
| `11-intelligence-twins.sql` | customer_twins, twin_simulations | 数字孪生 |

**RLS 工作原理**（面试必问）：
```sql
-- 每次查询前设置租户上下文
SET app.tenant_id = 'xxx';

-- RLS 策略自动过滤: 只返回当前租户的数据
CREATE POLICY tenant_isolation ON leads
  USING (tenant_id = current_setting('app.tenant_id')::uuid);

-- 效果: SELECT * FROM leads → 等价于 SELECT * FROM leads WHERE tenant_id = 'xxx'
-- 即使应用层忘记过滤，数据库层也能保证隔离
```

---

### 4.6 OPA 策略 `policies/`

| 文件 | 核心规则 | 被谁调用 |
|------|----------|----------|
| `common/tenant_isolation.rego` | 拒绝跨租户访问 | Gateway opa.ts (每次请求) |
| `common/rbac.rego` | 基于角色: admin/manager/agent/viewer 权限矩阵 | Gateway opa.ts |
| `common/abac.rego` | 基于属性: 时间/IP/数据敏感度/置信度 | Gateway opa.ts |
| `agents/core.rego` | Agent 能力边界: 哪些 Agent 可执行哪些操作 | BaseAgent.check_policy() |
| `agents/approval.rego` | Agent 审批: 高风险操作需人工审批 | BaseAgent.check_policy() |
| `knowledge.rego` | 知识管理权限: 谁可创建/审核/发布 | Gateway knowledge 路由 |
| `twins.rego` | 数字孪生访问限制 | Gateway twins 路由 |

---

### 4.7 可观测性 `observability/`

| 文件 | 作用 |
|------|------|
| `prometheus.yml` | Scrape 配置: gateway:4000/metrics, agents:5010/metrics, postgres/redis/kafka exporters |
| `alerts/platform-alerts.yaml` | 告警规则: 错误率>5%、P99延迟>2s、Kafka Lag>1000 等 |
| `slo/platform-slos.yaml` | SLO: 可用性>99.9%、P99延迟<500ms |
| `grafana/dashboards/` | 7 个仪表盘: 平台总览、AI 治理、混沌、成本、灾备、缓存、语音 |

---

### 4.8 部署 `deploy/`

| 文件 | 作用 |
|------|------|
| `helm/enterprise-crm/Chart.yaml` | Helm Chart，依赖 Bitnami: postgresql@14, redis@18, kafka@26, keycloak@17 |
| `helm/enterprise-crm/values.yaml` | 生产配置: 副本数、资源限制、HPA、PDB、nodeAffinity、tls、secrets |
| `templates/gateway.yaml` | Gateway Deployment + Service + HPA |
| `templates/agents.yaml` | Agents Deployment + Service |
| `templates/ingress.yaml` | Nginx Ingress (TLS 终止) |

---

### 4.9 其他

| 目录/文件 | 作用 |
|-----------|------|
| `docker-compose.yml` | 本地开发全栈: frontend:3000, gateway:4000, agents:5010, postgres:5432, redis:6379, kafka:9094, opa:8181, ollama:11434, weaviate:8082, keycloak:8081, prometheus:9090, grafana:3001, loki:3100 |
| `docker-compose.chaos.yml` | 混沌工程: 注入网络延迟、容器故障 |
| `schemas/events/` | 15 种事件的 JSON Schema (版本化，如 lead.created v1/v2) |
| `scripts/` | 运维脚本: 合规报告、灾备恢复、缓存基准、平台成熟度 |
| `tests/` | 跨域集成测试: GDPR、缓存安全、数据导出、灾备 |
| `docs/` | 架构文档 + 运维手册 (runbooks) |
| `.github/workflows/` | CI/CD: ci-cd.yml, chaos-tests.yml, tenant-isolation.yml |

---

## 五、Kafka 事件流全景图

```
Gateway 发布事件 (写入时):
  crm.leads.created ──────────────────┐
  crm.leads.updated ──────────────────┤
  crm.deals.created ──────────────────┤
  crm.deals.stage-changed ────────────┤
  crm.tickets.created ────────────────┤
  crm.tickets.updated ────────────────┤
  crm.tickets.resolved ───────────────┤
  crm.approvals.required ─────────────┤
  crm.approvals.decision ─────────────┤
  crm.automation.simulation.requested ┤
  crm.knowledge.draft.created ────────┤
                                      │
                    Kafka (20+ Topics)│
                                      │
Agent 消费事件 ←──────────────────────┤
  Agent Router → 分派到对应 Handler   │
                                      │
Agent 发布事件 (处理结果):            │
  crm.agents.action-proposed ─────────┤
  crm.agents.reasoning ──────────────┤
  crm.productivity.signal ───────────┤
  crm.productivity.action-suggested ─┤
  crm.journey.updated ───────────────┤
  crm.analytics.prediction-generated ┤
  crm.automation.simulation.result ───┤
  crm.automation.executed ───────────┤
  crm.knowledge.published ───────────┤
  crm.intelligence.search-performed ─┤
  crm.intelligence.voice-received ───┤
                                      │
Gateway 消费事件 ←────────────────────┤
  → 更新 Prisma 读模型 (Approval,    │
    Prediction, ProductivityProposal, │
    AutomationSimulation, etc.)       │
  → WebSocket 推送前端               │
  → Redis 缓存失效                   │
                                      ┘
```

---

## 六、核心架构模式详解

### 6.1 CQRS + 事件溯源

```
写端 (Core Services FastAPI):
  Client → POST /commands/leads
    → EventStore.append()  // 追加事件
    → Outbox.insert()      // 同事务写入发件箱
    → 返回 {aggregate_id, version}

发件箱发布器:
  while True:
    → SELECT from outbox_events WHERE published_at IS NULL FOR UPDATE SKIP LOCKED
    → KafkaProducer.send(topic, payload)
    → UPDATE published_at = now()

读端 (Gateway Prisma):
  Client → GET /api/v1/leads
    → Prisma db.lead.findMany({where: {tenantId}})  // 直接查读模型

事件投影:
  Kafka Consumer → 更新 Prisma 读模型 (实时)
  Replay Service → 从事件重建读模型 (回放)
```

**优势**：写端和读端独立扩展、事件可回放、天然审计日志

### 6.2 多租户隔离（三层防御）

```
第1层 - 数据库 RLS:
  SET app.tenant_id = 'xxx';
  → PostgreSQL 自动过滤所有查询

第2层 - 应用层:
  Gateway: tenantMiddleware 从 JWT 提取 tenantId
  → Prisma: where: {tenantId: req.tenantId}
  → Kafka: event.tenantid = req.tenantId
  → WebSocket: 订阅验证 tenant 前缀

第3层 - 策略层:
  OPA tenant_isolation.rego: 拒绝跨租户访问
  SecureCache: 租户隔离的缓存键命名空间

测试: test_tenant_isolation.py, test_replay_tenant_isolation.py, test_cache_isolation.py
```

### 6.3 AI 治理四层防线

```
第1层 - KillSwitch (急停开关):
  Agent 执行前 → kill_switch.decision(tenant_id, agent_id)
  → 查 Redis: global → tenant → agent 三级状态
  → blocked → 暂停 Kafka 分区消费 / 抛出 GovernanceBlocked

第2层 - Approval (审批工作流):
  高风险操作 → request_approval()
  → emit_event("crm.approvals.required") → Kafka → Gateway → Approval 表
  → 前端审批队列显示 → 人工审批
  → emit_event("crm.approvals.decision") → Kafka → Agent 执行/取消

第3层 - DataGuard (数据守护):
  emit_event 前 → data_guard.ensure_allowed()
  → 检查 GDPR 擦除: 用户已被遗忘 → 阻止
  → 检查 PII: 敏感字段脱敏
  → 检查保留策略: 数据已过期 → 阻止

第4层 - Explainability (可解释性):
  Agent 决策后 → explainability.record_decision()
  → 写入 agent_decisions 表
  → 前端 ExplainabilityPanel 展示: 推理过程、置信度、证据
```

### 6.4 弹性设计

```
CircuitBreaker:
  Closed → 连续 N 次失败 → Open (拒绝调用)
  Open → 超时后 → HalfOpen (允许1次试探)
  HalfOpen → 成功 → Closed / 失败 → Open

应用: Outbox Publisher 调用 Kafka 时使用

RetryPolicy:
  指数退避: delay = min(cap, base * 2^retry) + jitter
  最大重试次数 + 最大总耗时

混沌工程:
  docker-compose.chaos.yml 注入故障
  tests/chaos/ 测试: Kafka 宕机、DB 不可用、Redis 故障
```

---

## 七、面试高频问题与代码定位

| 问题 | 答案要点 | 关键代码位置 |
|------|----------|-------------|
| 多租户如何实现？ | PG RLS + 应用层 tenantId + OPA 策略 + 缓存隔离 | `02-rls-policies.sql`, `middleware/tenant.ts`, `common/tenant_isolation.rego` |
| 事件如何保证不丢？ | 事务性发件箱 (同事务写 event+outbox) + 轮询发布 + CircuitBreaker + DLQ | `write/outbox.py`, `services/outbox_publisher.py` |
| AI 治理怎么做的？ | KillSwitch+审批+数据守护+可解释性，每步可独立禁用 | `governance/guard.py`, `governance/kill_switch.py`, `base.py:emit_event()` |
| CQRS 读写分离？ | 写端 FastAPI+EventStore，读端 Prisma，Kafka 消费者同步 | `core_services/src/api.py`, `gateway/src/consumers/` |
| 缓存如何安全？ | 租户隔离键、epoch 版本化失效、OPA 缓存 Fail-Closed | `cache/secure_cache.py`, `middleware/opa.ts` |
| 事件回放？ | 从 event_store 重放历史事件，Projector 重建读模型，快照加速 | `replay/replay_service.py`, `replay/read_model_projector.py` |
| LLM 怎么调用的？ | Ollama 本地部署 llama3.1，ChatAgent 用 LangGraph 编排 | `base.py:call_llm()`, `chat/chat_agent.py` |
| WebSocket 如何安全？ | JWT 认证、租户隔离订阅 (验证 topic 前缀)、心跳检测 | `services/websocket.ts` |

---

## 八、推荐学习顺序

1. **docker-compose.yml** — 了解全栈服务组成
2. **database/migrations/** — 从 01 到 11 顺序阅读，理解数据模型演进
3. **gateway/src/index.ts** — Gateway 启动流程
4. **gateway/src/middleware/** — auth → tenant → opa → audit 中间件链
5. **gateway/src/services/kafka.ts** — 事件发布与 Topic 定义
6. **gateway/src/routes/leads.ts** — 典型 CRUD + 事件发布路由
7. **gateway/src/consumers/approvalsRequired.ts** — 典型 Kafka 消费者
8. **gateway/src/services/websocket.ts** — WebSocket 安全与推送
9. **frontend/src/lib/api.ts** — 前端 API 客户端，对应所有后端路由
10. **agents/src/orchestrator/main.py** — Agent 编排器启动
11. **agents/src/orchestrator/router.py** — 事件路由与分派
12. **agents/src/agents/base.py** — Agent 基类（治理+LLM+事件发布）
13. **agents/src/governance/** — KillSwitch → Approval → DataGuard → Explainability
14. **agents/src/intelligence/chat/** — Chat Agent (LangGraph+Ollama+Weaviate)
15. **core_services/src/api.py** — CQRS 写端
16. **core_services/src/write/** — EventStore + Outbox
17. **services/outbox_publisher.py** — 发件箱发布器
18. **policies/** — OPA Rego 策略
19. **tests/** — 集成测试
