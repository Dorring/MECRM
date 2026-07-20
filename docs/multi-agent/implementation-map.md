# MECRM 多智能体架构升级 — 实施映射

> **基线:** 2026-07-20 审计 (`docs/multi-agent/current-state-audit.md`)
> **目标:** 从"并列 Agent"升级为"具备 Supervisor、Specialist、Reviewer、受控 Executor 和可量化评测的企业级多智能体应用"
> **核心原则:** 两条路径长期共存 — Deterministic Kafka Handler 不迁移、Supervisor Multi-Agent 为独立新入口

---

## 1. 目标架构概述

### 1.1 两条长期共存的执行路径

```
                        ┌─────────────────────────┐
                        │     Kafka Event Bus      │
                        └─────┬──────────────┬─────┘
                              │              │
              ┌───────────────▼──┐   ┌───────▼────────────────┐
              │  Deterministic   │   │  Supervisor Multi-Agent │
              │  Kafka Handler   │   │  Graph (独立入口)        │
              │  (保持不变)        │   │                         │
              │                  │   │  POST /api/agents/       │
              │  固定事件/SLA/    │   │    objectives            │
              │  审批/审计/       │   │  或                       │
              │  生命周期更新      │   │  crm.agent.objective     │
              │                  │   │    .requested Topic       │
              └──────────────────┘   └─────┬───────────────────┘
                                           │
                              ┌────────────▼────────────┐
                              │    Complexity Gate       │  ← Phase 3
                              │    (simple → handler     │
                              │     complex → graph)     │
                              └────────────┬────────────┘
                                           │
                              ┌────────────▼────────────┐
                              │    Planner + Plan        │  ← Phase 3
                              │    Validator             │
                              └────────────┬────────────┘
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
            ┌───────▼──────┐    ┌─────────▼──────┐    ┌──────────▼──────┐
            │  Specialist   │    │   Specialist    │    │   Specialist     │
            │  (5 个旗舰)    │    │   (5 个旗舰)     │    │   (5 个旗舰)      │
            └───────┬──────┘    └─────────┬──────┘    └──────────┬──────┘
                    │                      │                      │
                    └──────────────────────┼──────────────────────┘
                                           │
                              ┌────────────▼────────────┐
                              │       Reviewer           │  ← Phase 6
                              │  (Evidence Grounding,    │
                              │   完整性, 冲突, Action)    │
                              └────────────┬────────────┘
                                           │
                              ┌────────────▼────────────┐
                              │   GovernedExecutor       │  ← Phase 7
                              │  (approve-gate → verify) │
                              └─────────────────────────┘
```

### 1.2 两条路径的分工

| 事件类型 | 走 Deterministic Kafka Handler | 走 Supervisor Graph |
|---|---|---|
| `crm.leads.created` (单实体 CRUD 触发) | ✅ | — |
| `crm.tickets.sla-breached` (SLA 报警) | ✅ | — |
| `crm.approvals.decision` (审批结果) | ✅ | — |
| `crm.audit.events` (审计事件) | ✅ | — |
| `crm.deals.closed` (生命周期) | ✅ | — |
| "分析客户续约意向，生成挽留方案" (跨域目标) | — | ✅ |
| "评估本月销售 Pipeline 健康度" (综合判断) | — | ✅ |
| "排查某客户投诉升级原因" (根因分析) | — | ✅ |

### 1.3 独立入口

Supervisor Multi-Agent 通过**新增独立入口**进入，不修改 `AgentRouter.route()`：

- `POST /api/agents/objectives` — HTTP API，接受 `{objective, context, constraints}`，返回 `{run_id}`
- `crm.agent.objective.requested` — Kafka Topic，异步入口

**现有 `AgentRouter.route()` 不无条件进入 Supervisor。**

---

## 2. 现有模块 → 目标模块映射

### 2.1 Agent 映射

| 现有模块 | 目标角色 | 映射方式 | Phase |
|---|---|---|---|
| `SalesAgent` | **Specialist:Sales** (Adapter) | Adapter 包装，保留 `qualify_lead`/`analyze_deal` | Phase 4 |
| `SupportAgent` | **Specialist:Support** (Adapter) | Adapter 包装，保留 `triage_ticket`/`suggest_resolution` | Phase 4 |
| `ComplianceAgent` | **作为 Reviewer 的输入/工具之一** | ❌ 不直接等同于 Reviewer。Reviewer 需要独立执行 Evidence Grounding、完整性、冲突和 Action 校验；ComplianceAgent 提供规则式 PII 检测能力 | Phase 6 |
| `AnalyticsAgent` | **Specialist:Analytics** (Adapter) | Adapter 包装，保留 `generate_forecast`/`track_*` | Phase 4 |
| `ProductivitySignalsAgent` | 保留在 Deterministic Handler | 不迁移 | — |
| `ProductivityAgent` | 保留在 Deterministic Handler | 不迁移 | — |
| `JourneyAgent` | 保留在 Deterministic Handler | 不迁移 | — |
| `PredictiveAnalyticsAgent` | 保留在 Deterministic Handler | 不迁移 | — |
| `AutomationSimulationAgent` | 保留在 Deterministic Handler | 不迁移 | — |
| `AutomationExecutorAgent` | **领域 Execute Adapter 或 Tool** | ❌ 不直接升级成统一 Executor。新增 `GovernedExecutor`；现有 `AutomationExecutorAgent` 仅作为自动化领域的 Execute Adapter 复用 | Phase 7 |
| `KnowledgeAgent` | **Specialist:Knowledge** (Adapter) | Adapter 包装 | Phase 4 |
| `ChatAgent` (HTTP) | 保留在 HTTP API | 不迁移 | — |
| `SearchAgent` (HTTP) | 保留在 HTTP API | 不迁移 | — |
| `ComplianceIntelligenceAgent` (HTTP) | 保留在 HTTP API | 不迁移 | — |
| **不存在** | **Supervisor Agent** | 新增：LangGraph StateGraph + AgentNode 路由 + Complexity Gate | Phase 5 |
| **不存在** | **Reviewer Agent** | 新增：独立验证层（Evidence Grounding、完整性、冲突、Action 校验） | Phase 6 |
| **不存在** | **GovernedExecutor** | 新增：approve-gate + verify + Proposal Hash | Phase 7 |

### 2.2 旗舰场景 5 个 Specialist Adapter（Phase 4）

第一版**不实现全部 11 个 Specialist Adapter**。旗舰场景仅包含：

| Specialist | 复用的现有 Agent | 核心能力 |
|---|---|---|
| **Customer Context** | `CustomerProfile` + `CustomerTimeline` 查询 | 客户画像、历史交互、当前阶段 |
| **Support** | `SupportAgent` | 工单分类、解决方案推荐、KB 检索 |
| **Sales** | `SalesAgent` | 线索评分、交易分析、下一步建议 |
| **Knowledge** | `KnowledgeAgent` | KB 文章检索、草稿生成 |
| **Analytics** | `AnalyticsAgent` | 预测、异常检测、趋势分析 |

### 2.3 AgentRouter 拆分方案

`orchestrator/router.py` 的拆分：

| 保留部分 | 目标位置 | 说明 |
|---|---|---|
| `AgentRouter.routes` 字典 | 保留在 `router.py` | 23 topic → handler 映射，不迁移 |
| `AgentRouter.route()` 方法 | 保留为 Deterministic 入口 | 不修改 |
| 所有 handler 方法 | 保留在 `router.py` | 固定事件/SLA/审批/审计/生命周期继续走此路径 |
| Governance 组件初始化 | 提取到 `multi_agent/shared/governance.py` | 供两套路径共享 |

| 新增部分（不修改 router.py） | 目标位置 |
|---|---|
| Supervisor 入口 handler | `multi_agent/supervisor/entrypoint.py` |
| 新 HTTP 路由 `POST /api/agents/objectives` | `agents/src/orchestrator/main.py`（新增路由） |
| 新 Kafka Consumer `crm.agent.objective.requested` | `multi_agent/supervisor/consumer.py` |

| Router DI 迁移 | 作为独立、可回归测试的改动 |
|---|---|
| 44 行 setter 注入 → DI 容器 | 在 Phase 4 后评估，独立 PR |

### 2.4 存储分类（四种不同语义的存储，不可混淆）

| 存储类型 | 现有表/机制 | 用途 | 生命周期 |
|---|---|---|---|
| **Checkpoint** (运行时恢复状态) | **不存在**，需新增 `langgraph_checkpoints` | LangGraph 图状态快照，用于暂停/恢复和 Interrupt | 短期（任务生命周期内），可过期清理 |
| **Run Evidence** (审计和可观测性) | `agent_decisions`、`agent_events`、OpenTelemetry | 每次决策的推理、证据、工具调用、Trace 关联 | 长期（与租户数据保留策略对齐） |
| **Business Task** (业务状态) | `agent_tasks` | 面向用户的任务状态、输入/输出、correlation_id | 长期（业务数据） |
| **Memory** (长期上下文) | `ai_memory`、Weaviate ChatMemory | Agent 跨会话记忆、知识积累 | 长期（用户可控删除） |

**这四类存储不可混淆为同一种。** LangGraph Checkpoint 是运行时恢复状态，不能替代 Run Evidence 的审计功能；Business Task 是面向用户的任务视图，不能替代 Checkpoint 的精确状态恢复。

### 2.5 双写一致性（开放设计问题）

Checkpoint 写入 PG 和事件发送到 Kafka 无法在同一事务中完成。

**不走"顺序双写"简易方案。** 后续通过以下技术组合解决：
- Proposal Hash（审批前后 payload 一致性校验）
- Idempotency Key（Kafka 消息去重）
- Outbox 模式（已有 `outbox_events` 表可供复用，写 Checkpoint 后将事件写入 Outbox，独立 Publisher 发送）
- 执行后 Verify（Executor 执行后验证结果与 Proposal 一致）
- 必要时引入 PG 事务包裹 Checkpoint + Outbox 写入（两者同库可原子提交）

具体方案在 Phase 7 中设计。

---

## 3. 各阶段涉及的具体文件

### Phase 1：AI_MODE 和 Provider 运行边界

**目标：** 建立 `AI_MODE`（none/static/supervisor）运行模式开关；统一所有 `OLLAMA_*` env var 读取到 Provider 边界；Provider 健康检查。

```
新增:
  agents/src/orchestrator/ai_mode.py               # AI_MODE enum + feature flags
  tests/unit/test_provider_boundary.py              # Provider 边界单元测试

修改:
  agents/src/orchestrator/config.py                 # 新增 AI_MODE 环境变量
  agents/src/intelligence/providers.py              # 新增 provider_health_check()
  agents/src/intelligence/chat/chat_agent.py        # 替换 os.getenv("OLLAMA_EMBED_MODEL") → create_embeddings()
  agents/src/intelligence/knowledge/knowledge_agent.py  # 同上
  agents/src/intelligence/search/search_agent.py    # 同上
  agents/src/orchestrator/main.py                   # 同上
  agents/src/intelligence/i18n/voice_ingest.py      # 抽象 Whisper backend 选择
```

### Phase 2：Contracts 和未接线的 Agent Registry

**目标：** 统一 `AgentResponse` Schema；创建 `AgentRegistry`（注册但不替换 Router 的运行时 DI）；`SpecialistCapability` 声明。

```
新增:
  agents/src/multi_agent/__init__.py
  agents/src/multi_agent/shared/schema.py           # AgentResponse, AgentAction, SpecialistCapability
  agents/src/multi_agent/registry.py                # AgentRegistry（独立于 Router）

修改:
  agents/src/agents/base.py                         # 添加 agent_response() 辅助方法（可选）

不修改:
  agents/src/orchestrator/router.py                 # DI 注入保持不变
```

### Phase 3：Complexity Gate、Planner 和 Plan Validator

**目标：** 请求复杂度评估；Plan 结构生成；确定性 Plan 验证器。

```
新增:
  agents/src/multi_agent/supervisor/complexity_gate.py   # 复杂度评估（规则式）
  agents/src/multi_agent/supervisor/planner.py           # Plan 生成
  agents/src/multi_agent/supervisor/plan_validator.py    # Plan 结构验证（确定性）
  agents/src/multi_agent/supervisor/plan_schema.py       # Objective, Plan, Task Graph Schema
  tests/unit/test_plan_validator.py                      # Plan 验证器测试
```

### Phase 4：旗舰场景 5 个 Specialist Adapter

**目标：** 为 5 个旗舰场景创建 Adapter——Customer Context、Support、Sales、Knowledge、Analytics。

```
新增:
  agents/src/multi_agent/specialist/__init__.py
  agents/src/multi_agent/specialist/base.py              # SpecialistAgent(ABC)
  agents/src/multi_agent/specialist/registry.py          # SpecialistRegistry
  agents/src/multi_agent/specialist/adapters/__init__.py
  agents/src/multi_agent/specialist/adapters/customer_context.py
  agents/src/multi_agent/specialist/adapters/support.py
  agents/src/multi_agent/specialist/adapters/sales.py
  agents/src/multi_agent/specialist/adapters/knowledge.py
  agents/src/multi_agent/specialist/adapters/analytics.py
  tests/unit/test_specialist_adapters.py                 # Adapter 行为测试

不修改:
  agents/src/agents/sales.py                             # 原 SalesAgent 不变
  agents/src/agents/support.py                           # 原 SupportAgent 不变
  agents/src/agents/analytics.py                         # 原 AnalyticsAgent 不变
```

### Phase 5：独立入口的 Supervisor Graph

**目标：** 新增 `POST /api/agents/objectives` 和 `crm.agent.objective.requested` Topic；Supervisor StateGraph。

```
新增:
  agents/src/multi_agent/supervisor/graph.py             # Supervisor StateGraph
  agents/src/multi_agent/supervisor/state.py             # SupervisorState
  agents/src/multi_agent/supervisor/routing.py           # Complexity Gate → Plan → Specialist 选择
  agents/src/multi_agent/supervisor/entrypoint.py        # HTTP + Kafka 入口

修改:
  agents/src/orchestrator/main.py                        # 新增 POST /api/agents/objectives 路由
  agents/src/orchestrator/config.py                      # 新增 crm.agent.objective.requested topic
```

### Phase 6：Reviewer 和有限 Replan

**目标：** 独立 Reviewer Agent（Evidence Grounding、完整性、冲突、Action 校验）；最多 1 次 Replan。

```
新增:
  agents/src/multi_agent/reviewer/__init__.py
  agents/src/multi_agent/reviewer/base.py                # ReviewerAgent(ABC)
  agents/src/multi_agent/reviewer/contracts.py           # EvidenceGrounding / Completeness / Conflict / Action 合同
  agents/src/multi_agent/reviewer/evaluator.py           # 合同评估器
  agents/src/multi_agent/supervisor/replan.py            # 有限 Replan 逻辑（最多 1 次）
  tests/unit/test_reviewer_contracts.py
```

### Phase 7：Postgres Checkpointer、Human Approval、GovernedExecutor 和 Verify

**目标：** PostgresSaver；Interrupt + 审批集成；GovernedExecutor（approve-gate + Proposal Hash + verify）。

```
新增:
  agents/src/multi_agent/shared/checkpointer.py          # PostgresSaver 工厂
  agents/src/multi_agent/executor/__init__.py
  agents/src/multi_agent/executor/base.py                # GovernedExecutor(ABC)
  agents/src/multi_agent/executor/approve_gate.py        # 审批门控
  agents/src/multi_agent/executor/proposal_hash.py       # Proposal Hash 计算与校验
  database/migrations/13-langgraph-checkpoint.sql        # LangGraph checkpoints 表

依赖新增:
  langgraph-checkpoint-postgres>=2.0
```

### Phase 8：Run Trace 和确定性 Demo

**目标：** 统一 Run/DAG/Task/Handoff/Token/Cost/Checkpoint Trace；可重复端到端演示。

```
新增:
  agents/src/multi_agent/shared/tracer.py                # RunTraceCollector
  agents/src/multi_agent/shared/trace_schema.py          # RunTrace / TaskTrace / HandoffTrace
  scripts/demo-supervisor-workflow.sql                   # 确定性 Demo 种子数据

修改:
  agents/src/intelligence/evaluation/workflow_trace.py   # 扩展 Trace 合同覆盖
```

### Phase 9：三组评测

**目标：** Single Agent 回归评测；Static Workflow 评测；Supervisor Multi-Agent 端到端评测。

```
新增:
  evals/single_agent_regression.py                       # Single Agent 回归
  evals/static_workflow_eval.py                          # Static Workflow 评测
  evals/supervisor_multi_agent_eval.py                   # Supervisor 端到端评测
  evals/datasets/multi_agent_scenarios.jsonl             # 多 Agent 场景用例
```

---

## 4. 可复用的现有数据结构

### 4.1 数据库表（无需修改）

| 现有表 | 复用方式 |
|---|---|
| `ai_agents` | 新增 Supervisor/Reviewer/GovernedExecutor 行 |
| `agent_tasks` | 作为 **Business Task** 存储（面向用户的任务视图） |
| `agent_events` | 作为 **Run Evidence** 存储（事件类型记录） |
| `agent_decisions` | 作为 **Run Evidence** 存储（Reviewer 决策记录） |
| `approvals` | 作为 GovernedExecutor approve-gate 的审批记录 |
| `audit_logs` | 保持不变 |
| `ai_memory` | 作为 **Memory** 存储（长期上下文） |
| `outbox_events` | 作为 Checkpoint → Event 的事务性发件箱 |

### 4.2 现有 Pydantic 模型

| 模型 | 复用方式 |
|---|---|
| `ChatIntent` | 扩展为 Complexity Gate 的意图分类输入 |
| `SearchIntent` | 复用为 Specialist 参数 |
| `DecisionArtifact` | 复用为 Run Evidence 写入基础 |
| `PendingAction` | 复用为 GovernedExecutor 预执行存储 |
| `ResolutionSuggestion` / `ResolutionStep` | 保持不变，作为 Specialist 内部验证 |
| `ForecastResult` / `ForecastPrediction` | 保持不变，作为 Specialist 内部验证 |
| `ActionProposal` / `ProposedWrite` | 复用为 GovernedExecutor Proposal 格式 |

### 4.3 需要新增的 Schema

| 新增 Schema | 用途 | Phase |
|---|---|---|
| `AgentResponse` (Pydantic) | 统一所有 Agent 的返回格式 | Phase 2 |
| `AgentAction` (Pydantic) | Supervisor 向 Specialist 的委托指令 | Phase 2 |
| `SpecialistCapability` (Pydantic) | Specialist 能力声明 | Phase 2 |
| `Objective` (Pydantic) | 用户/系统提交的跨域目标 | Phase 3 |
| `Plan` / `TaskNode` (Pydantic) | Planner 生成的执行计划 | Phase 3 |
| `ReviewResult` (Pydantic) | Reviewer 合同检查输出 | Phase 6 |
| `ProposalHash` (Pydantic) | GovernedExecutor 审批前后校验 | Phase 7 |
| `RunTrace` / `TaskTrace` / `HandoffTrace` (Pydantic) | 统一多 Agent Trace | Phase 8 |

---

## 5. Feature Flag 设计

```bash
# Phase 1
AI_MODE=none                     # none | static | supervisor
                                 # none: 现有行为不变
                                 # static: 使用 AgentRegistry + AgentResponse 但路由不变
                                 # supervisor: 启用 Supervisor Graph 入口

# Phase 5
SUPERVISOR_ENTRY_ENABLED=false   # POST /api/agents/objectives 开关

# Phase 8
RUN_TRACE_ENABLED=false          # 统一 Run Trace 收集开关
```

---

## 6. 风险控制矩阵（扩展）

| # | 风险 | 严重程度 | 缓解措施 | Phase |
|---|---|---|---|---|
| R1 | 循环依赖 | 中 | Supervisor 图禁止 Specialist → Supervisor 边；PlanValidator 验证无环 | Phase 3 |
| R2 | 同步/异步边界 | 中 | 所有 LangGraph 节点为 async；Kafka 发送通过 Outbox 解耦 | Phase 7 |
| R3 | 双写原子性 | **高** | Proposal Hash + Idempotency Key + Outbox + 执行后 Verify；PG 事务包裹 Checkpoint+Outbox | Phase 7 |
| R4 | Schema 重复 | 低 | 四类存储清晰分类；新增 Checkpoint 表与现有表互补不重叠 | Phase 7 |
| R5 | Provider 耦合 | 中 | Phase 1 统一所有 env var 读取到 Provider 边界 | Phase 1 |
| R6 | 测试环境依赖 | 中 | 新增测试使用相同 `tests/conftest.py` 夹具 | 全 Phase |
| R7 | 前端兼容性 | 低 | 新增 type 值，不破坏现有 icon 映射 | Phase 5 |
| R8 | 治理注入冗余 | 中 | Router DI 迁移作为独立改动，先建 AgentRegistry 再评估替换时机 | Phase 2→4 |
| R9 | Agent ID 冲突 | 低 | `AgentRegistry.register()` 检查唯一性 | Phase 2 |
| R10 | OPA 策略盲区 | 中 | 新增 Agent 角色时同步更新 `policies/agents/core.rego` | Phase 5/6/7 |
| R11 | **无限循环与成本失控** | **高** | Planner 设置 `max_steps`；Supervisor 设置 `token_budget`；每步有成本归因 | Phase 3/5 |
| R12 | **并行竞态与部分失败** | **高** | Specialist 并行执行结果合并策略（all_succeed / any_succeed / majority）；部分失败时触发 Replan 或降级 | Phase 5/6 |
| R13 | **Tenant Context 与 Foreign Evidence** | **高** | 所有 Specialist 检索和 Reviewer 验证强制 `SET app.tenant_id`；跨租户证据泄露作为评测硬性失败 | Phase 4/6/9 |
| R14 | **审批后 Proposal 篡改** | 中 | GovernedExecutor 计算 Proposal Hash，与审批时的 Hash 比对；不匹配则拒绝执行 | Phase 7 |
| R15 | **Kafka Replay 和重复副作用** | 中 | 所有 emit_event 携带 Idempotency Key；Executor 在执行前检查 Key 是否已处理 | Phase 7 |
| R16 | **Shadow Mode 双执行** | 中 | `AI_MODE=shadow` 时两条路径并行执行、比对结果、仅旧路径生效；适用于迁移验证 | Phase 5+ |
| R17 | **Checkpoint 数据保留 / GDPR** | 中 | Checkpoint 数据 TTL 与租户数据保留策略对齐；PII 字段标记 + 擦除 API | Phase 7 |
| R18 | **Prompt/Schema/Registry 版本漂移** | 中 | `SpecialistCapability` 包含 `schema_version` 和 `prompt_version`；Supervisor 路由前校验版本兼容性 | Phase 2/5 |
| R19 | **Trace PII 与高基数指标** | 中 | Trace 中的 prompt/response 不存储原文，仅存储 hash；Agent ID × Objective ID 聚合，禁止 Tenant ID × Task ID 标签 | Phase 8 |
| R20 | **Reviewer 相关性错误** | 中 | Reviewer 合同定义与 Plan 结构对齐；合同检查结果可被 Run Trace 回溯 | Phase 6/8 |
| R21 | **Live Model 非确定性** | 中 | Complexity Gate 使用确定性规则；Plan Validator 不调用 LLM；Reviewer 关键检查使用规则式；Phase 9 评测统计多次运行的通过率 | Phase 3/6/9 |
| R22 | **P95 延迟与单位任务成本** | 中 | Phase 5 起记录每 Task 的 wall-clock 和 token cost；Phase 9 评测报告 P50/P95/P99 延迟和单位成本 | Phase 5/9 |
| R23 | **Agent Capability 与 Tool 权限不一致** | 中 | `SpecialistCapability` 声明与实际 Tool 列表在注册时交叉校验；OPA `agents/core.rego` 中能力与权限联合定义 | Phase 2/4 |

---

## 7. 验证清单

每个 Phase 完成后需验证：

- [ ] 现有 23 个 Kafka Topic 消费者未被修改或行为变化
- [ ] `docker-compose up -d`（不含 `--profile local-llm`）可正常启动
- [ ] `AI_PROVIDER=nvidia_nim` 配置下所有 Agent 可正常工作
- [ ] `pytest tests -v` 全部通过（agents）
- [ ] `cd gateway && npm test` 全部通过
- [ ] `cd frontend && npm run build` 成功
- [ ] `GET /api/v1/agents` 返回数据格式不变
- [ ] `GET /api/v1/agents/workflows/recent` 返回数据格式不变
- [ ] OPA 策略检查不受影响
- [ ] Kill Switch 行为不变
- [ ] RLS 租户隔离未被绕过
- [ ] 审计日志持续记录
- [ ] `python -m src.orchestrator.main` 可正常启动
- [ ] 无新增 `os.getenv("OLLAMA_*")` 调用（Phase 1 后）
