# MECRM 多智能体架构升级 — 实施映射

> **基线:** 2026-07-20 审计 (`docs/multi-agent/current-state-audit.md`)
> **目标:** 从"并列 Agent"升级为"具备 Supervisor、Specialist、Reviewer、受控 Executor 和可量化评测的企业级多智能体应用"
> **原则:** 不重构现有 Agent、不修改业务代码、不破坏运行路径

---

## 1. 目标架构概述

```
┌─────────────────────────────────────────────────────┐
│                   Kafka Event Bus                    │
└──────────┬──────────────────────────────────────────┘
           │
    ┌──────▼──────┐
    │  Supervisor  │  ← 新：LangGraph StateGraph
    │  Agent       │    含 Checkpointer + Interrupt
    └──┬───┬───┬──┘
       │   │   │
  ┌────▼┐ ┌▼──┐ ┌▼─────┐
  │Spec │ │Spc│ │Spec   │  ← 迁移：现有 Agent 通过 Adapter 复用
  │Sales│ │Sup│ │Compl  │
  └──┬──┘ └┬──┘ └──┬───┘
     │      │       │
  ┌──▼──────▼───────▼──┐
  │     Reviewer        │  ← 新：合同检查 + OPA + 质量评分
  └────────┬───────────┘
           │
  ┌────────▼───────────┐
  │  Executor (gated)   │  ← 新：approve-gate → emit_event
  └─────────────────────┘
           │
  ┌────────▼───────────┐
  │  Evaluation Layer   │  ← 扩展：LLM Judge + Trace 评测
  └─────────────────────┘
```

---

## 2. 现有模块 → 目标模块映射

### 2.1 Agent 映射

| 现有模块 | 目标角色 | 映射方式 | 说明 |
|---|---|---|---|
| `SalesAgent` | **Specialist:Sales** | Adapter 包装 | 保留 `qualify_lead`、`analyze_deal`、`recommend_next_action`；通过 `SalesSpecialistAdapter` 暴露统一 `process(event) -> AgentResponse` |
| `SupportAgent` | **Specialist:Support** | Adapter 包装 | 保留 `triage_ticket`、`suggest_resolution`；`SupportSpecialistAdapter` 统一接口 |
| `ComplianceAgent` | **Specialist:Compliance** + **Reviewer 基础** | 双重角色 | `validate_data` 既是 Specialist 能力，也是 Reviewer 的验证逻辑来源 |
| `AnalyticsAgent` | **Specialist:Analytics** | Adapter 包装 | 保留 `track_*`、`generate_forecast` 方法 |
| `ProductivitySignalsAgent` | **Specialist:Productivity** | Adapter 包装 | `ingest_event` 能力 |
| `ProductivityAgent` | **Specialist:Productivity** | Adapter 包装 | `handle_signal` 能力 |
| `JourneyAgent` | **Specialist:Journey** | Adapter 包装 | `process` 已符合抽象接口 |
| `PredictiveAnalyticsAgent` | **Specialist:PredictiveAnalytics** | Adapter 包装 | 预测能力 |
| `AutomationSimulationAgent` | **Specialist:Automation** | Adapter 包装 | 模拟能力 |
| `AutomationExecutorAgent` | **Executor 基础** | 直接升级 | 已有 `execute` 语义，升级为受控 Executor |
| `KnowledgeAgent` | **Specialist:Knowledge** | Adapter 包装 | KB 管理能力 |
| `ChatAgent` (HTTP) | **Specialist:Chat** | Adapter 包装 | 对话能力，HTTP 接口保持不变 |
| `SearchAgent` (HTTP) | **Specialist:Search** | Adapter 包装 | 检索能力，HTTP 接口保持不变 |
| `ComplianceIntelligenceAgent` (HTTP) | **Specialist:AuditSearch** | Adapter 包装 | 审计搜索，HTTP 接口保持不变 |
| `AgentRouter` | **Supervisor（部分保留）+ Router（部分废弃）** | 拆分 | 见 2.2 |
| **不存在** | **Supervisor Agent** | 新增 | LangGraph StateGraph + AgentNode 路由 |
| **不存在** | **Reviewer Agent** | 新增 | 合同验证 + OPA + 质量评分 |
| **不存在** | **Executor Agent** | 新增 | approve-gate → 安全执行 |

### 2.2 AgentRouter 拆分方案

`orchestrator/router.py` (599 行) 将拆分为：

| 保留部分 | 目标位置 | 说明 |
|---|---|---|
| `AgentRouter.routes` 字典 | `multi_agent/topic_registry.py` | Topic → handler 映射表，保持不变 |
| `AgentRouter.route()` 方法 | 保留为兼容入口 | 调用 Supervisor Graph |
| Governance 组件初始化 | `multi_agent/governance_provider.py` | 提取到 DI 容器 |
| 44 行 setter 注入 | `multi_agent/agent_factory.py` | 改为注册时自动注入 |
| `_blocked` / `_blocked_data` 辅助函数 | `multi_agent/guards.py` | 提取为公共函数 |
| `_tenant_id` / `_extract_subjects` | `multi_agent/event_utils.py` | 提取为公共函数 |

| 废弃/重构部分 | 替代方案 |
|---|---|
| 手动 Agent 实例化 (11 行) | `AgentRegistry` 注册 + DI 容器 |
| 手动 handler 中的 Agent 串联调用 | Supervisor LangGraph 中的条件边 |
| `_handle_approval_decision` 中的 4-agent 硬编码 dispatch | `AgentRegistry.get(agent_id)` 通用查找 |
| handler 中的 governance 检查重复代码 | Supervisor 图中统一 pre-node guard |

### 2.3 新增目录结构

```
agents/src/multi_agent/
  __init__.py
  supervisor/
    __init__.py
    graph.py              # Supervisor StateGraph
    state.py              # SupervisorState dataclass
    routing.py            # Intent → Specialist 路由逻辑
  specialist/
    __init__.py
    base.py               # SpecialistAgent 基类
    registry.py           # Specialist 注册表
    adapters/
      __init__.py
      sales.py            # SalesSpecialistAdapter
      support.py          # SupportSpecialistAdapter
      compliance.py       # ComplianceSpecialistAdapter
      analytics.py        # AnalyticsSpecialistAdapter
      productivity.py     # ProductivitySpecialistAdapter
      journey.py          # JourneySpecialistAdapter
      automation.py       # AutomationSpecialistAdapter
      knowledge.py        # KnowledgeSpecialistAdapter
      chat.py             # ChatSpecialistAdapter
      search.py           # SearchSpecialistAdapter
  reviewer/
    __init__.py
    base.py               # ReviewerAgent 基类
    contracts.py          # 合同定义 (Pydantic)
    evaluator.py          # 合同评估器
  executor/
    __init__.py
    base.py               # ExecutorAgent 基类
    approve_gate.py       # 审批门控
  shared/
    __init__.py
    schema.py             # 统一 AgentResponse / AgentAction
    checkpointer.py       # PostgresSaver 工厂
    tracer.py             # Run Trace 记录器
    governance.py         # 共享治理注入
  registry.py             # AgentRegistry
  factory.py              # AgentFactory (DI 容器)
  topic_registry.py       # Kafka Topic 映射表（从 router.py 迁移）
  event_utils.py          # 事件辅助函数
  guards.py               # Governance 检查辅助
```

---

## 3. 可复用的现有数据结构

### 3.1 数据库表（无需修改）

| 现有表 | 复用方式 |
|---|---|
| `ai_agents` | 保持不变 — 新增 Supervisor/Reviewer/Executor 行 |
| `agent_tasks` | 复用为 LangGraph 任务节点记录；`correlation_id` 用于 Trace 关联 |
| `agent_events` | 复用为 LangGraph 步骤事件记录 |
| `agent_decisions` | 复用为 Reviewer 决策记录；`evidence` JSON 字段可直接存储合同检查结果 |
| `approvals` | 复用为 Executor approve-gate；`requestor_type: "agent"` 路径已存在 |
| `audit_logs` | 保持不变 |
| `ai_memory` | 复用为 Supervisor 上下文记忆 |

### 3.2 现有 Pydantic 模型

| 模型 | 复用方式 |
|---|---|
| `ChatIntent` | 扩展为 Supervisor 意图分类输出 |
| `SearchIntent` | 复用为 Specialist 参数 |
| `DecisionArtifact` | 复用为 Reviewer 输出基础 |
| `PendingAction` | 复用为 Executor 预执行存储 |
| `ResolutionSuggestion` / `ResolutionStep` | 保持不变，作为 Specialist 内部验证 |
| `ForecastResult` / `ForecastPrediction` | 保持不变，作为 Specialist 内部验证 |
| `ActionProposal` / `ProposedWrite` | 复用为 Executor 输出格式 |

### 3.3 需要新增的 Schema

| 新增 Schema | 用途 |
|---|---|
| `AgentResponse` (Pydantic) | 统一所有 Agent 的返回格式 |
| `AgentAction` (Pydantic) | Supervisor 向 Specialist 的委托指令 |
| `ReviewResult` (Pydantic) | Reviewer 合同检查输出 |
| `ExecutionReceipt` (Pydantic) | Executor 执行回执 |
| `SupervisorState` (dataclass) | LangGraph Supervisor 图状态 |
| `SpecialistCapability` (Pydantic) | Specialist 能力声明 |
| `HandoffRecord` (Pydantic) | Agent 间 handoff 记录 |

---

## 4. 各阶段涉及的具体文件

### Phase 0：基线保护（无行为变化）

**目标：** 建立统一 Schema + Agent 注册表，不改现有运行路径。

```
新增:
  agents/src/multi_agent/__init__.py
  agents/src/multi_agent/shared/schema.py           # AgentResponse, AgentAction
  agents/src/multi_agent/registry.py                # AgentRegistry
  agents/src/multi_agent/shared/governance.py       # GovernanceProvider (DI)

修改:
  agents/src/agents/base.py                         # 添加 agent_response() 辅助方法
  agents/src/orchestrator/router.py                 # 替换手动实例化为 AgentRegistry
  tests/infra/test_h2_multi_agent_trace_evaluation.py # 扩展测试
```

### Phase 1：状态基础设施

**目标：** LangGraph PostgresSaver + Checkpointer + Run Trace。

```
新增:
  agents/src/multi_agent/shared/checkpointer.py     # PostgresSaver 工厂
  agents/src/multi_agent/shared/tracer.py           # RunTraceCollector
  database/migrations/13-langgraph-checkpoint.sql   # LangGraph checkpoints 表（若需）

修改:
  agents/src/intelligence/chat/graph.py             # 添加 checkpointer（可选）
  agents/src/orchestrator/main.py                   # 启动时初始化 PostgresSaver

依赖新增:
  langgraph-checkpoint-postgres>=2.0
```

### Phase 2：角色引入

**目标：** Supervisor + Specialist + Reviewer + Executor 基类，与现有 Agent 并行。

```
新增:
  agents/src/multi_agent/supervisor/graph.py        # Supervisor StateGraph
  agents/src/multi_agent/supervisor/state.py        # SupervisorState
  agents/src/multi_agent/supervisor/routing.py      # 意图分类 → Specialist 选择
  agents/src/multi_agent/specialist/base.py         # SpecialistAgent(ABC)
  agents/src/multi_agent/specialist/registry.py     # SpecialistRegistry
  agents/src/multi_agent/specialist/adapters/*.py   # 11 个 Adapter
  agents/src/multi_agent/reviewer/base.py           # ReviewerAgent(ABC)
  agents/src/multi_agent/reviewer/contracts.py      # 合同 Schema
  agents/src/multi_agent/executor/base.py           # ExecutorAgent(ABC)
  agents/src/multi_agent/executor/approve_gate.py   # 审批门控
  policies/agents/supervisor.rego                   # Supervisor OPA 策略

修改:
  agents/src/orchestrator/router.py                 # 添加可选 Supervisor 路径（feature flag）
  policies/agents/core.rego                         # 新增 Supervisor/Reviewer/Executor 能力注册
```

### Phase 3：渐进迁移

**目标：** 将现有 Router handler 逻辑迁移到 Supervisor Graph。

```
修改:
  agents/src/orchestrator/router.py                 # 逐步将 topic handler → Supervisor 调用
  agents/src/orchestrator/main.py                   # 启动时创建 Supervisor 实例

可能废弃（feature flag 保护）:
  agents/src/orchestrator/router.py 中的 handler 方法  # 逐 topic 迁移
```

### Phase 4：评测扩展

**目标：** LLM Judge + 多 Agent Trace 评测 + 质量指标体系。

```
新增:
  evals/llm_judge.py                                # LLM Judge 评测
  evals/multi_agent_trace.py                        # 端到端多 Agent Trace 评测
  evals/datasets/multi_agent_cases.jsonl            # 多 Agent 测试用例
  evals/datasets/llm_judge_references.jsonl         # LLM Judge 参考答案

修改:
  agents/src/intelligence/evaluation/workflow_trace.py  # 扩展为多 Agent 合同
  evals/run_safety_contract_eval.py                 # 扩展覆盖新 Agent 角色
```

---

## 5. 审批决策复用方案

现有 `_handle_approval_decision`（router.py:498-553）中的 4-agent 硬编码 dispatch：

```python
# 现有代码（仅覆盖 4 个核心 Agent）
agent = {
    self.sales_agent.agent_id: self.sales_agent,
    self.support_agent.agent_id: self.support_agent,
    self.compliance_agent.agent_id: self.compliance_agent,
    self.analytics_agent.agent_id: self.analytics_agent,
}.get(pending.agent_id)
```

**升级方案：**
```python
# 通过 AgentRegistry 通用查找（覆盖所有已注册 Agent）
agent = self.registry.get(pending.agent_id)
```

`AgentRegistry` 在 Phase 0 中建立，Phase 2 中所有 Specialist Adapter 自动注册。

---

## 6. Topic 路由迁移方案

现有 23 个 Kafka Topic → handler 映射保持不变。迁移策略：

| 阶段 | 路由方式 |
|---|---|
| Phase 0-1 | `AgentRouter.route()` 不变，直接调用 handler |
| Phase 2 | 新增并行路径：部分 topic 同时走 Supervisor Graph（feature flag `SUPERVISOR_ENABLED_TOPICS`） |
| Phase 3 | 全部 topic 迁移到 Supervisor Graph；原 handler 降级为回退路径 |

---

## 7. Feature Flag 设计

```bash
# .env 中控制迁移节奏
SUPERVISOR_ENABLED=false                   # Phase 2 总开关
SUPERVISOR_ENABLED_TOPICS=crm.leads.created,crm.deals.created  # 逐 topic 启用
SUPERVISOR_CHECKPOINTER_ENABLED=false      # Phase 1 开关
SUPERVISOR_INTERRUPT_ENABLED=false         # Phase 2 开关（人在回路中断）
```

---

## 8. 风险控制矩阵

| 风险 | 缓解措施 |
|---|---|
| R1 循环依赖 | Supervisor 图中禁止 Specialist → Supervisor 边；`AgentRegistry` 验证无环 |
| R2 同步/异步边界 | 所有 LangGraph 节点为 `async`；Kafka 发送在事务后执行 |
| R3 Kafka 与 Graph 状态冲突 | 复用 `outbox_events` 表；Graph checkpoint 后写入 outbox，独立 publisher 发送 |
| R4 Schema 重复 | `AgentTask` 表新增 `graph_state_id` 外键，不创建重复表 |
| R5 Provider 耦合 | `SpecialistAdapter` 通过 `create_chat_model()` 获取 LLM，不直接依赖具体 Provider |
| R6 测试环境依赖 | 新增测试使用相同的 `tests/conftest.py` 夹具 |
| R7 前端兼容性 | 新增 Agent type 值（`supervisor`、`specialist`、`reviewer`、`executor`）；前端 `page.tsx` 扩展 icon 映射但不破坏现有 type |
| R8 治理注入冗余 | Phase 0 的 `GovernanceProvider` 替换 44 行 setter |
| R9 Agent ID 冲突 | `AgentRegistry.register()` 检查唯一性 |
| R10 OPA 策略盲区 | 每新增 Agent 角色时同步更新 `policies/agents/core.rego` |

---

## 9. 建议的实施顺序调整

基于审计发现，建议按以下优先级调整：

| 原计划 | 调整建议 | 原因 |
|---|---|---|
| 先建 Supervisor Graph | **先做 Phase 0 基线保护** | 现有 Agent 无统一 Schema，Supervisor 需要统一接口才能路由 |
| LangGraph Checkpointer 后做 | **Phase 1 提前** | PostgresSaver 是 Supervisor 持久化状态的硬依赖；且现有代码零影响 |
| 一次性迁移所有 Topic | **逐 Topic 渐进迁移** | Feature flag 保护，降低回滚风险 |
| LLM Judge 最后 | **Phase 2 同步启动 Reviewer 设计** | Reviewer 需要合同定义，可与 Judge 评测共享合同 Schema |

**最终推荐顺序：Phase 0 → Phase 1 → Phase 2 → Phase 3（逐 topic） → Phase 4**

---

## 10. 验证清单

实施每个 Phase 后需验证：

- [ ] 现有 23 个 Kafka Topic 的消费者未被修改
- [ ] `docker-compose up -d`（不含 `--profile local-llm`）可正常启动
- [ ] `pytest tests -v` 全部通过（agents）
- [ ] `cd gateway && npm test` 全部通过
- [ ] `cd frontend && npm run build` 成功
- [ ] `GET /api/v1/agents` 返回数据格式不变
- [ ] `GET /api/v1/agents/workflows/recent` 返回数据格式不变
- [ ] OPA 策略检查不受影响（无新增 deny）
- [ ] Kill Switch 行为不变（global/tenant/agent/tenant-agent 四级）
- [ ] RLS 租户隔离未被绕过
- [ ] 审计日志持续记录
- [ ] `python -m src.orchestrator.main` 可正常启动
