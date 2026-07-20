# MECRM 当前状态审计报告

> **审计日期:** 2026-07-20
> **审计范围:** 全仓库源码级审计（非 README 推断）
> **目标:** 为 Supervisor/Specialist/Reviewer/Executor 多智能体架构升级提供基线

---

## 1. 仓库现状摘要

### 1.1 Agent 体系总览

当前共有 **11 个 Agent**，全部继承自 `BaseAgent`（ABC），在 `AgentRouter` 中手动实例化：

| # | Agent 类 | 模块 | agent_id | 类型 |
|---|---|---|---|---|
| 1 | `SalesAgent` | `agents/src/agents/sales.py` | `sales-agent` | 核心 Agent |
| 2 | `SupportAgent` | `agents/src/agents/support.py` | `support-agent` | 核心 Agent |
| 3 | `ComplianceAgent` | `agents/src/agents/compliance.py` | `compliance-agent` | 核心 Agent |
| 4 | `AnalyticsAgent` | `agents/src/agents/analytics.py` | `analytics-agent` | 核心 Agent |
| 5 | `ProductivitySignalsAgent` | `intelligence/productivity/` | `productivity-signals-agent` | 智能 Agent |
| 6 | `ProductivityAgent` | `intelligence/productivity/` | `productivity-agent` | 智能 Agent |
| 7 | `JourneyAgent` | `intelligence/journey/` | `journey-agent` | 智能 Agent |
| 8 | `PredictiveAnalyticsAgent` | `intelligence/analytics/` | `predictive-analytics-agent` | 智能 Agent |
| 9 | `AutomationSimulationAgent` | `intelligence/automation/` | `automation-simulation-agent` | 智能 Agent |
| 10 | `AutomationExecutorAgent` | `intelligence/automation/` | `automation-executor-agent` | 智能 Agent |
| 11 | `KnowledgeAgent` | `intelligence/knowledge/` | `knowledge-agent` | 智能 Agent |

另有通过 HTTP API 直接调用的 Agent（不在 Kafka 消费链路中）：
- `ChatAgent` — `intelligence/chat/chat_agent.py`
- `SearchAgent` — `intelligence/search/search_agent.py`
- `ComplianceIntelligenceAgent` — `intelligence/compliance/compliance_agent.py`
- `DevExperienceAgent` — `intelligence/devx/devx_agent.py`

### 1.2 编排模型

**单一 `AgentRouter` 类** (`orchestrator/router.py`) 负责全部编排：

- 11 个 Agent 在 `__init__` 中手动 `AgentClass()` 实例化
- 5 个治理组件（KillSwitch、GovernanceGuard、ApprovalService、ExplainabilityEngine、DataGuard）在 `__init__` 中实例化
- `initialize()` 方法包含 **44 行 setter 注入代码**（11 agent × 4 治理组件），全部硬编码
- 23 个 Kafka Topic 通过 `self.routes: Dict[str, Callable]` 映射到 handler 方法
- **没有**：
  - 依赖注入容器
  - Agent 注册表/发现机制
  - 装饰器注册
  - 动态路由
  - 工厂模式

**Agent 间通信仅通过 Kafka 事件**——一个 Agent 发送事件到 Topic，下游 Agent 消费。没有 Agent-to-Agent 直接调用或委托。

### 1.3 Agent 调用模式

Router 的 handler 方法直接调用 Agent 的**领域特定方法**，而非通过 `process()` 抽象接口：

```python
# router.py _handle_lead_created()
await self.sales_agent.qualify_lead(event)        # 直接调用
await self.compliance_agent.validate_data(event)   # 直接调用

# router.py _handle_deal_created()
await self.journey_agent.process(event)            # 唯一走抽象接口的调用
await self.sales_agent.analyze_deal(event)         # 直接调用
await self.compliance_agent.validate_data(event)   # 直接调用
```

**关键发现：`BaseAgent.process()` 抽象方法定义存在，但 Router 从不通过它调用核心 Agent。** 仅 `JourneyAgent` 通过 `process()` 被调用。

### 1.4 LangGraph 使用情况

仓库中共有 **12 个 LangGraph 图**，全部为 `StateGraph`：

| 模块 | 图 | 节点数 | 模式 |
|---|---|---|---|
| `chat/graph.py` | `build_chat_graph` | 6 | 线性 DAG |
| `search/graph.py` | `build_search_graph` | 6 | 线性 DAG |
| `i18n/graph.py` | `build_i18n_ingest_graph` | 3 | 线性 DAG |
| `i18n/graph.py` | `build_i18n_response_graph` | 1 | 单节点 |
| `automation/graph.py` | `build_automation_graph` | 2 | 线性 DAG |
| `productivity/graph.py` | `build_productivity_graph` | 5 | 线性 DAG |
| `analytics/graph.py` | `build_analytics_graph` | 1 | 单节点 |
| `journey/graph.py` | `build_journey_graph` | 2 | 线性 DAG |
| `knowledge/graph.py` | `build_knowledge_draft_graph` | 2 | 线性 DAG |
| `compliance/graph.py` | `build_audit_search_graph` | 1 | 单节点 |
| `devx/graph.py` | `build_devx_graph` | 4 | 线性 DAG |
| `twins/graph.py` | `build_twin_graph` | 3 | 线性 DAG |

**关键缺失：**
- ❌ **无 Checkpointer** — 所有图 `compile()` 均未传入 `checkpointer` 参数
- ❌ **无 Interrupt** — 所有图均未使用 `interrupt_before`/`interrupt_after`
- ❌ **无 MemorySaver** / `SqliteSaver` / `PostgresSaver`
- ❌ **无条件边** — 所有图为严格线性 DAG，无分支、循环
- ❌ **无 Supervisor Graph** — 没有多 Agent 协调图模式
- ❌ **无 Run Trace** — 没有使用 LangGraph 的 `Command` streaming 或 tracing

**LangGraph 当前本质上是顺序管道编排器**——相同的功能可以用 `async/await` 函数组合实现。

### 1.5 模型 Provider

**单一边界**：`intelligence/providers.py` 提供 `create_chat_model()` 和 `create_embeddings()` 工厂函数。

| 特性 | 状态 |
|---|---|
| 支持 Provider | `ollama`（默认）、`nvidia_nim` |
| 切换方式 | `AI_PROVIDER` 环境变量 |
| Ollama 默认启用 | ❌ 否 — docker-compose.profiles: `["local-llm"]` |
| Ollama 隐式依赖 | ❌ 否 — 通过 factory 抽象，切换后无需改业务代码 |
| 参数名误导 | ⚠️ 部分构造函数参数名为 `ollama_url`，但功能上安全 |
| ChatOpenAI 使用 | 仅在 `AI_PROVIDER=nvidia_nim` 时延迟导入 |
| AzureOpenAI | ❌ 不存在 |

**结论：Ollama 仅在显式启用 `local-llm` profile 时启动，代码不硬编码连接 Ollama。**

### 1.6 治理链路总览

| 组件 | 位置 | 实现 |
|---|---|---|
| **Kill Switch** | `agents/governance/kill_switch.py` + `gateway/routes/governance.ts` | Redis 4 作用域（global/tenant/agent/tenant-agent），pub/sub 通知 |
| **OPA 策略** | `policies/*.rego`（10 个文件） | 每次请求 3 策略并行检查（tenant_isolation + rbac + abac） |
| **审批流程** | `agents/governance/approval_service.py` + `gateway/routes/approvals.ts` + `policies/agents/approval.rego` | Redis 待处理队列 + Kafka `crm.approvals.required/decision` 事件 |
| **RLS** | `database/migrations/02-rls-policies.sql` | 32+ 表 `FORCE ROW LEVEL SECURITY`，`app.tenant_id` 上下文 |
| **审计链路** | `gateway/middleware/audit.ts` → Kafka → `consumers/auditEvents.ts` → `audit_logs` 表 | 写操作捕获（请求体已脱敏），幂等写入 |
| **Explainability** | `agents/governance/explainability.py` → `agent_decisions` 表 | 每次 Agent 决策记录 reasoning + evidence + tool_calls |
| **Data Guard** | `agents/governance/data_guard.py` | 客户/用户 opt-out 检查，软删除感知 |
| **Input Safety** | `agents/governance/input_safety.py` | 不可信文本评估（注入检测） |

### 1.7 Agent 输出 Schema

**没有统一的 Agent 输出 Schema。**

- `BaseAgent.process()` 返回 `Dict[str, Any]`，各 Agent 返回异构字典
- 通用状态字段：`status` ∈ {`completed`, `failed`, `denied`, `skipped`, `pending_approval`}
- Kafka 事件使用 CloudEvents 1.0 信封（标准化）
- **仅 2 个 Agent 使用 Pydantic 进行 LLM 输出验证**（不是 Agent 自身返回值）：
  - `SupportAgent` → `ResolutionSuggestion`, `ResolutionStep`
  - `AnalyticsAgent` → `ForecastResult`, `ForecastPrediction`
- 其余 Agent 使用自制的 JSON 解析辅助函数（`_parse_json_response`, `_extract_json_object`）

### 1.8 评测体系

**当前评测位于 `evals/` 和 `intelligence/evaluation/`：**

| 评测 | 类型 | 覆盖 | 不覆盖 |
|---|---|---|---|
| 安全合约评测 (`evals/run_safety_contract_eval.py`) | 确定性 | prompt 注入阻断、证据引用、工具路由合约、run_status | — |
| 结构化检索评测 (`evals/run_structured_retrieval_eval.py`) | PostgreSQL 实际 | recall@5、precision@5、跨租户隔离、租户泄露 | 语义检索质量 |
| 工作流 Trace 评测 (`intelligence/evaluation/workflow_trace.py`) | 确定性合同 | 任务完成顺序、handoff 正确性、policy-before-action | — |

**当前评测不可覆盖的能力：**
- ❌ LLM 生成质量（答案准确性、幻觉率）
- ❌ 语义检索质量（NDCG、MRR）
- ❌ 多 Agent 协作质量（端到端成功率、handoff 延迟）
- ❌ 延迟 SLA（P50/P95/P99）
- ❌ Token 使用优化
- ❌ Agent 决策正确性（业务层面）
- ❌ 人在回路交互质量

---

## 2. 仅存在于文档中的能力

经源码核实，以下能力**仅在 README/文档中提及，未在代码中实现**：

| 声称的能力 | 实际状态 |
|---|---|
| "LangGraph 多 Agent 协作" | LangGraph 仅用作线性管道，无 Supervisor/AgentNode |
| "Agent 协作工作流" | 实际是 AgentRouter 手动串联 Agent 调用，Agent 间无直接通信 |
| "Checkpointer/持久化状态" | 不存在 — 所有图 `compile()` 无 checkpointer |
| "Human-in-loop via LangGraph interrupt" | 不存在 — 审批通过 Kafka 事件 + Redis 队列实现，非 LangGraph interrupt |
| "Agent 动态发现/注册" | 不存在 — 全部手动硬编码实例化 |
| "统一 Agent 输出 Schema" | 不存在 — 各 Agent 返回异构 dict |

---

## 3. 关键文件路径清单

### 3.1 Agent 定义
```
agents/src/agents/base.py              # BaseAgent 抽象类
agents/src/agents/__init__.py          # 导出 4 个核心 Agent
agents/src/agents/sales.py             # SalesAgent
agents/src/agents/support.py           # SupportAgent（含 Pydantic 输出验证）
agents/src/agents/compliance.py        # ComplianceAgent（规则式 PII 检测）
agents/src/agents/analytics.py         # AnalyticsAgent（含 Forecast Pydantic 模型）
```

### 3.2 编排与路由
```
agents/src/orchestrator/main.py        # AgentOrchestrator 入口 + HTTP 服务器
agents/src/orchestrator/router.py      # AgentRouter：11 Agent 初始化 + 23 topic 路由
agents/src/orchestrator/config.py      # Settings：所有环境变量
```

### 3.3 Provider 边界
```
agents/src/intelligence/providers.py   # create_chat_model / create_embeddings 工厂
```

### 3.4 治理
```
agents/src/governance/kill_switch.py      # AgentKillSwitch (Redis 4 作用域)
agents/src/governance/guard.py            # GovernanceGuard
agents/src/governance/approval_service.py # ApprovalService (Redis 队列)
agents/src/governance/explainability.py   # ExplainabilityEngine (PG 决策记录)
agents/src/governance/data_guard.py       # DataGuard (opt-out 检查)
agents/src/governance/input_safety.py     # InputSafetyDecision
agents/src/governance/agent_telemetry.py  # Prometheus 指标
agents/src/governance/evidence.py         # is_valid_run_status
```

### 3.5 网关（治理相关）
```
gateway/src/middleware/auth.ts         # JWT 验证 + 撤销
gateway/src/middleware/tenant.ts       # 租户解析
gateway/src/middleware/opa.ts          # OPA 3 策略并行检查
gateway/src/middleware/audit.ts        # 审计事件捕获
gateway/src/routes/agents.ts           # Agent API（只读）
gateway/src/routes/approvals.ts        # 审批 CRUD + decide
gateway/src/routes/governance.ts       # Kill Switch 控制
```

### 3.6 策略
```
policies/common/tenant_isolation.rego  # 租户隔离
policies/common/rbac.rego              # 角色权限
policies/common/abac.rego              # 属性访问控制
policies/agents/core.rego              # Agent 能力定义
policies/agents/approval.rego          # 审批阈值
policies/approval.rego                 # 通用审批
policies/knowledge.rego                # 知识库治理
policies/twins.rego                    # 数字孪生治理
policies/devx.rego                     # DevX 治理
```

### 3.7 数据库
```
gateway/prisma/schema.prisma           # 36 个 Prisma 模型
database/migrations/01-core-tables.sql # 核心表
database/migrations/02-rls-policies.sql# RLS 策略 (32+ 表)
database/migrations/09-agent-decisions.sql # Agent 决策表
database/migrations/10-data-governance.sql # 数据治理
database/migrations/11-intelligence-twins.sql # 智能与数字孪生
```

### 3.8 评测
```
evals/safety_contracts.py              # 安全合约评测
evals/structured_retrieval.py          # 结构化检索数据模型
evals/run_safety_contract_eval.py      # 安全评测 Runner
evals/run_structured_retrieval_eval.py # 检索评测 Runner
agents/src/intelligence/evaluation/workflow_trace.py          # Trace 合同评分
agents/src/intelligence/evaluation/evaluate_workflow_trace.py # Trace 评测 CLI
```

### 3.9 Docker / 配置
```
docker-compose.yml                     # 20 个服务（含 profile-gated ollama）
.env.example                           # 环境变量模板
```

---

## 4. 架构特征总结

### 4.1 可复用的现有基础设施

| 组件 | 复用价值 | 原因 |
|---|---|---|
| CloudEvents 1.0 事件信封 | ✅ 高 | 标准化，包含 tenantid + correlationid |
| `BaseAgent` 治理 setter 模式 | ✅ 高 | 可改为 DI 容器以消除硬编码 |
| `providers.py` Provider 边界 | ✅ 高 | 干净抽象，支持新增 Provider |
| `AgentKillSwitch` (Redis) | ✅ 高 | 4 作用域完整实现 |
| OPA 3 策略并行检查 | ✅ 高 | `agents/core.rego` 可直接扩展 |
| `ApprovalService` (Redis 队列) | ✅ 高 | 人在回路基础设施完整 |
| `ExplainabilityEngine` (PG) | ✅ 高 | 决策记录 Schema 完整 |
| `AgentDecision` / `AgentEvent` / `AgentTask` 表 | ✅ 高 | 可直接用于 Trace 存储 |
| RLS (32+ 表) | ✅ 高 | 无需修改 |
| 审计中间件 + audit_logs 表 | ✅ 高 | 无需修改 |
| `HybridRetriever` (结构化+语义+知识) | ✅ 中 | RAG 基础存在 |
| 评测 Runner 框架 | ✅ 中 | 可扩展评测维度 |

### 4.2 需要新增的能力

| 能力 | 当前状态 | 目标 |
|---|---|---|
| Agent 注册表/发现 | ❌ 手动实例化 | `AgentRegistry` 或 DI 容器 |
| 统一 Agent 输出 Schema | ❌ 异构 dict | Pydantic `AgentResponse` |
| Supervisor 路由逻辑 | ❌ 手动 handler | Supervisor Agent + LangGraph StateGraph |
| Specialist 委托模式 | ❌ Router 直接调用 | Specialist Agent 注册 + 能力匹配 |
| Reviewer 验证模式 | ❌ 不存在 | Reviewer Agent + OPA/合同检查 |
| Executor 安全执行 | ❌ 部分（approval 后 emit_event） | 受控 Executor，带 approve-gate |
| LangGraph Checkpointer | ❌ 不存在 | PostgresSaver / MemorySaver |
| LangGraph Interrupt | ❌ 不存在 | 人在回路中断点 |
| Run Trace | ❌ 不存在 | LangGraph tracing 或自定义 |
| Agent 输出质量评测 | ❌ 不存在 | LLM Judge / 对比评测 |
| 多 Agent Trace 评测 | ⚠️ 仅合同评分 | 端到端工作流正确性 |

---

## 5. 风险清单

| # | 风险 | 严重程度 | 描述 |
|---|---|---|---|
| R1 | **循环依赖** | 中 | 若 Supervisor 调用 Specialist 而 Specialist 回调 Supervisor，需在设计层面禁止回环。当前 AgentRouter 是单向的，改造后需显式禁止。 |
| R2 | **同步/异步边界** | 中 | Kafka 消费者是异步的，LangGraph StateGraph 需要同步状态传递。若在图中调用 `await producer.send()`，需要确保状态持久化与事件发送的一致性。 |
| R3 | **Kafka 与 LangGraph 状态冲突** | 高 | 当前 Agent 通过 Kafka 事件通信，LangGraph Checkpointer 通过 PG 持久化状态。若两者不同步（图状态已更新但事件未发出），会导致状态不一致。需实现事务性发件箱（已有 OutboxEvent 表可供复用）。 |
| R4 | **数据库 Schema 重复** | 低 | 已有 `agent_tasks`、`agent_events`、`agent_decisions` 表。新增 Supervisor trace 表时注意与现有表字段关系，避免重复。 |
| R5 | **Provider 耦合** | 中 | `create_chat_model()` 当前返回 LangChain `BaseChatModel`。若 Specialist 需要不同的 temperature/model 配置，需扩展 factory 而非新增 Provider 依赖。 |
| R6 | **测试环境依赖** | 中 | 现有测试依赖 Docker Compose（Postgres、Redis、OPA），新增测试需保持同等隔离级别。 |
| R7 | **前端 API 兼容性** | 低 | `GET /api/v1/agents` 和 `GET /api/v1/agents/workflows/recent` 返回的字段需保持向后兼容。若新增 Supervisor Agent 类型，前端 Agent 卡片需支持新 type。 |
| R8 | **治理注入冗余** | 中 | 当前 44 行 setter 注入难以维护。若新增 Specialist 类型，需改为注册时自动注入。 |
| R9 | **Agent ID 冲突** | 低 | 新增 Agent 的 `agent_id` 需保持唯一，建议使用注册表统一管理。 |
| R10 | **OPA 策略盲区** | 中 | 当前 OPA 策略仅覆盖 4 个核心 Agent 的能力。新增 Specialist/Executor 需在 `policies/agents/core.rego` 中注册能力。 |

---

## 6. 建议的后续实施顺序

基于风险评估，建议：

1. **Phase 0 — 基线保护**：添加 Agent 输出 Pydantic Schema + Agent 注册表。不改行为。
2. **Phase 1 — 状态基础设施**：LangGraph PostgresSaver + Checkpointer + Run Trace。不改编排逻辑。
3. **Phase 2 — 角色引入**：Supervisor + Specialist + Reviewer Agent 基类。与现有 Agent 并行运行。
4. **Phase 3 — 迁移**：将现有 Router 逻辑迁移到 Supervisor Graph。渐进式替换。
5. **Phase 4 — 评测扩展**：LLM Judge + 多 Agent Trace 评测。

**当前阶段完成后建议的下一步：Phase 0（基线保护），因为它是无破坏性的基础改造。**
