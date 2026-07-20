# MECRM 当前状态审计报告

> **审计日期:** 2026-07-20
> **审计范围:** 全仓库源码级审计（非 README 推断）
> **目标:** 为 Supervisor/Specialist/Reviewer/受控 Executor 多智能体架构升级提供基线
> **分支:** `chore/ma-00-baseline-audit-v2`

---

## 1. 仓库现状摘要

### 1.1 Agent 体系总览

全仓库共有 **至少 12 个 `BaseAgent` 子类**，其中 11 个在 `AgentRouter` 中手动实例化并接入 Kafka 消费链路。

**复现命令：**
```bash
grep -r "class.*Agent(BaseAgent)" agents/src/ --include="*.py"       # 12 个子类
grep -E "= \w+Agent\(\)" agents/src/orchestrator/router.py           # 11 个在 Router 中
```

**AgentRouter 中的 11 个 Agent：**

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

**Router 之外的第 12 个 `BaseAgent` 子类：**
- `DevExperienceAgent` (`intelligence/devx/devx_agent.py`) — 不通过 Kafka 消费，通过 HTTP API 直接调用

另有通过 HTTP API 直接调用的 Agent（不继承 `BaseAgent`，不在 Kafka 消费链路中）：
- `ChatAgent` — `intelligence/chat/chat_agent.py`
- `SearchAgent` — `intelligence/search/search_agent.py`
- `ComplianceIntelligenceAgent` — `intelligence/compliance/compliance_agent.py`

### 1.2 编排模型

**单一 `AgentRouter` 类** (`orchestrator/router.py`) 负责全部编排：

- 11 个 Agent 在 `__init__` 中手动 `AgentClass()` 实例化
- 5 个治理组件（KillSwitch、GovernanceGuard、ApprovalService、ExplainabilityEngine、DataGuard）在 `__init__` 中实例化
- `initialize()` 方法包含 **44 行 setter 注入代码**（11 agent × 4 治理组件），全部硬编码

**复现命令：**
```bash
grep -cE "set_(governance_guard|data_guard|approval_service|explainability_engine)" \
  agents/src/orchestrator/router.py   # 44
```

- 23 个 Kafka Topic 通过 `self.routes: Dict[str, Callable]` 映射到 handler 方法

**复现命令：**
```bash
grep -c "crm\." agents/src/orchestrator/config.py   # 23
```

- **没有**：依赖注入容器、Agent 注册表/发现机制、装饰器注册、动态路由、工厂模式

**Agent 间通信存在两种模式，而非仅 Kafka：**

1. **进程内顺序调用** — Router handler 方法在同一进程内依次调用多个 Agent 的领域方法（如 `_handle_deal_created` 中先 `journey_agent.process()` 再 `sales_agent.analyze_deal()` 再 `compliance_agent.validate_data()`）。调用结果通过返回值传递，但 Router 不检查下游 Agent 的返回值来决定是否继续。

2. **Kafka 跨业务流程事件传播** — Agent 通过 `BaseAgent.emit_event()` 发送 CloudEvents 到 Kafka Topic，由其他 Consumer 或后续事件触发新的消费。

**当前真正缺少的是：动态 Handoff（运行时决定委托给谁）、共享任务状态（多 Agent 可读写同一任务上下文）、结构化结果依赖（声明式"B 依赖 A 的输出"）、以及重规划（Plan → 执行 → 发现缺口 → 修订 Plan）。**

### 1.3 Agent 调用模式

Router 的 handler 方法**多数**直接调用 Agent 的领域特定方法，少数通过 `process()` 抽象接口。

`process()` 调用统计（共 7 处）：

**复现命令：**
```bash
grep -n "\.process(" agents/src/orchestrator/router.py
```

- `JourneyAgent.process()` — 在 6 个 handler 中被调用（ticket_created、ticket_updated、deal_created、deal_stage_changed、deal_updated/deal_closed/sla-breached/customers/payments 共用 handler、approval_decision）
- `PredictiveAnalyticsAgent.process()` — 在 1 个 handler 中被调用（journey_updated）

```python
# router.py _handle_deal_created() — 混合调用模式
await self.productivity_signals_agent.ingest_event(...)  # 领域方法
await self.journey_agent.process(event)                   # 抽象接口
await self.sales_agent.analyze_deal(event)                # 领域方法
await self.compliance_agent.validate_data(event, ...)     # 领域方法
```

**关键发现：`BaseAgent.process()` 抽象方法存在，但仅有 JourneyAgent 和 PredictiveAnalyticsAgent 通过它被调用。** 核心 Agent（Sales、Support、Compliance、Analytics）的 `process()` 实现虽然存在，Router 却绕过它直接调用领域方法。

### 1.4 LangGraph 使用情况

仓库中共有 **至少 12 个 LangGraph 图**（`StateGraph` 实例），分布在 11 个文件中（`i18n/graph.py` 包含 2 个图）。

**复现命令：**
```bash
grep -rn "StateGraph(" agents/src/ --include="*.py"   # 12 处
```

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

**复现命令：**
```bash
grep -rn "checkpointer\|MemorySaver\|SqliteSaver\|PostgresSaver" agents/src/ --include="*.py"
# (no matches)
```

- ❌ **无 Interrupt** — 所有图均未使用 `interrupt_before`/`interrupt_after`

**复现命令：**
```bash
grep -rn "interrupt_before\|interrupt_after\|\.interrupt" agents/src/ --include="*.py"
# (no matches)
```

- ❌ **无条件边** — 所有图为严格线性 DAG，无分支、循环
- ❌ **无 Supervisor Graph** — 没有多 Agent 协调图模式

**关于 Run Trace 的准确描述：**

已有基础设施：
- ✅ **OpenTelemetry** — `agents/requirements.txt` 包含 `opentelemetry-api>=1.21.0`、`opentelemetry-sdk>=1.43.0`；Gateway 有 `@opentelemetry/auto-instrumentations-node`
- ✅ **Agent Decision 记录** — `ExplainabilityEngine` 将每次 Agent 决策写入 `agent_decisions` 表（含 reasoning、evidence、tool_calls、approval_id、correlation_id）
- ✅ **Agent Event 记录** — `AgentEvent` 表记录事件类型、推理、置信度、审批状态
- ✅ **Agent Task 记录** — `AgentTask` 表记录任务状态、输入/输出、correlation_id
- ✅ **审计日志** — `audit_logs` 表记录所有写操作

**缺少的是统一的多智能体 Trace：**
- ❌ 无 Run/DAG 级别 Trace（一次 Objective → Completion 的完整图遍历记录）
- ❌ 无 Task/Handoff 级别 Trace（Supervisor→Specialist→Reviewer→Executor 的委托链）
- ❌ 无 Replan 记录（初始 Plan vs 修订 Plan 的 diff）
- ❌ 无 Token/Cost 归因（每次 LLM 调用的 token 消耗归属于哪个 Task/Agent/Step）
- ❌ 无 Checkpoint Trace（LangGraph 状态快照与业务事件的关联）
- ❌ 现有 OpenTelemetry 未与 Agent 决策链路打通

**LangGraph 当前本质上是顺序管道编排器**——相同的功能可以用 `async/await` 函数组合实现。

### 1.5 模型 Provider

**单一边界**：`intelligence/providers.py` 提供 `create_chat_model()` 和 `create_embeddings()` 工厂函数。

**复现命令：**
```bash
grep -n "def create_chat_model\|def create_embeddings" agents/src/intelligence/providers.py
# create_chat_model (line 158), create_embeddings (line 185)
```

| 特性 | 状态 |
|---|---|
| 支持 Provider | `ollama`（默认）、`nvidia_nim` |
| 切换方式 | `AI_PROVIDER` 环境变量 |
| Ollama 服务默认启动 | ❌ 否 — docker-compose.profiles: `["local-llm"]`，需 `docker compose --profile local-llm up -d ollama` |
| Chat Model 延迟创建 | ✅ 是 — `BaseAgent.call_llm()` 中首次调用时 `self._llm = create_chat_model(temperature=0)` |
| 运行时默认 `AI_PROVIDER` | ⚠️ 仍为 `ollama`（`config.py` 第 45 行 `os.getenv("AI_PROVIDER", "ollama")`） |
| 首次模型调用可能连接未启动的 Ollama | ⚠️ 是 — 若 `AI_PROVIDER=ollama`（默认）但 Ollama 容器未启动，`ChatOllama.ainvoke()` 将因连接拒绝失败 |
| 历史检索模块 Ollama 专用命名 | ⚠️ 存在 — 多个模块的构造函数参数名为 `ollama_url`，且部分模块绕过 Settings 直接读取 `OLLAMA_*` 环境变量 |

**绕过 Provider 边界直接读取 Ollama 环境变量的位置（隐式耦合）：**

**复现命令：**
```bash
grep -rn "os.getenv.*OLLAMA\|os.environ.*OLLAMA" agents/src/ --include="*.py"
```

| 文件 | 直接读取的变量 | 风险 |
|---|---|---|
| `intelligence/chat/chat_agent.py:47` | `OLLAMA_EMBED_MODEL` | 绕过 `settings` 和 `create_embeddings()` |
| `intelligence/knowledge/knowledge_agent.py:43` | `OLLAMA_EMBED_MODEL` | 同上 |
| `intelligence/search/search_agent.py:47` | `OLLAMA_EMBED_MODEL` | 同上 |
| `orchestrator/main.py:474` | `OLLAMA_EMBED_MODEL` | 同上 |
| `intelligence/i18n/voice_ingest.py:51` | `OLLAMA_URL` | 直接拼接 Whisper API URL |
| `intelligence/i18n/voice_ingest.py:87-88` | — | `_transcribe_ollama()` 硬编码 Ollama multimodal 端点 |

**结论：Ollama 服务默认不启动；Chat Model 通过延迟 Provider Factory 创建；但运行时默认 `AI_PROVIDER` 仍为 `ollama`；首次真实模型或 Embedding 调用可能连接未启动的 Ollama；部分历史检索模块仍保留 Ollama 专用配置命名和直接环境变量读取。**

### 1.6 治理链路总览

| 组件 | 位置 | 实现 |
|---|---|---|
| **Kill Switch** | `agents/governance/kill_switch.py` + `gateway/routes/governance.ts` | Redis 4 作用域（global/tenant/agent/tenant-agent），pub/sub 通知 |
| **OPA 策略** | `policies/*.rego`（至少 11 个文件） | 每次请求 3 策略并行检查（tenant_isolation + rbac + abac） |
| **审批流程** | `agents/governance/approval_service.py` + `gateway/routes/approvals.ts` + `policies/agents/approval.rego` | Redis 待处理队列 + Kafka `crm.approvals.required/decision` 事件 |
| **RLS** | `database/migrations/02-rls-policies.sql` | 至少 33 张表 `FORCE ROW LEVEL SECURITY`，`app.tenant_id` 上下文 |
| **审计链路** | `gateway/middleware/audit.ts` → Kafka → `consumers/auditEvents.ts` → `audit_logs` 表 | 写操作捕获（请求体已脱敏），幂等写入 |
| **Explainability** | `agents/governance/explainability.py` → `agent_decisions` 表 | 每次 Agent 决策记录 reasoning + evidence + tool_calls |
| **Data Guard** | `agents/governance/data_guard.py` | 客户/用户 opt-out 检查，软删除感知 |
| **Input Safety** | `agents/governance/input_safety.py` | 不可信文本评估（注入检测） |

**复现命令：**
```bash
find policies/ -name "*.rego" -type f | wc -l                              # 11
grep -Ec "^\s+'[a-z_]+'" database/migrations/02-rls-policies.sql           # 33
```

### 1.7 Agent 输出 Schema

**没有统一的 Agent 输出 Schema。**

- `BaseAgent.process()` 返回 `Dict[str, Any]`，各 Agent 返回异构字典
- 通用状态字段（非强制）：`status` ∈ {`completed`, `failed`, `denied`, `skipped`, `pending_approval`}
- Kafka 事件使用 CloudEvents 1.0 信封（标准化，在 `BaseAgent.emit_event()` 中构建）
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

| 声称的能力 | 实际状态 | 验证命令 |
|---|---|---|
| "LangGraph 多 Agent 协作" | LangGraph 仅用作线性管道，无 Supervisor/AgentNode | `grep -rn "StateGraph(" agents/src/` — 12 个图全部为单图线性 DAG |
| "Agent 协作工作流" | 实际是 AgentRouter 手动串联 Agent 调用，Agent 间无直接通信 | `grep -rn "class.*Agent(BaseAgent)"` — 无 Supervisor 或 Delegator 模式 |
| "Checkpointer/持久化状态" | 不存在 — 所有图 `compile()` 无 checkpointer | `grep -rn "checkpointer\|MemorySaver\|SqliteSaver\|PostgresSaver" agents/src/` — no matches |
| "Human-in-loop via LangGraph interrupt" | 不存在 — 审批通过 Kafka 事件 + Redis 队列实现，非 LangGraph interrupt | `grep -rn "interrupt_before\|interrupt_after" agents/src/` — no matches |
| "Agent 动态发现/注册" | 不存在 — 全部手动硬编码实例化 | `grep -rn "register\|Registry\|discover" agents/src/orchestrator/` — 无 Agent 注册机制 |
| "统一 Agent 输出 Schema" | 不存在 — 各 Agent 返回异构 dict | `grep -rn "class.*Response\|AgentOutput\|AgentResult" agents/src/agents/` — 无统一 BaseModel |

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
policies/common/tenant.rego            # 租户匹配
policies/agents/core.rego              # Agent 能力定义
policies/agents/approval.rego          # 审批阈值
policies/approval.rego                 # 通用审批
policies/knowledge.rego                # 知识库治理
policies/twins.rego                    # 数字孪生治理
policies/devx.rego                     # DevX 治理
policies/tests/tenant_isolation_test.rego  # 租户隔离测试
```
**精确数量：11 个 .rego 文件**

### 3.7 数据库
```
gateway/prisma/schema.prisma           # 35 个 Prisma 模型
database/migrations/01-core-tables.sql # 核心表
database/migrations/02-rls-policies.sql# RLS 策略（33 张表）
database/migrations/09-agent-decisions.sql # Agent 决策表
database/migrations/10-data-governance.sql # 数据治理
database/migrations/11-intelligence-twins.sql # 智能与数字孪生
```
**复现命令：**
```bash
grep -c "^model " gateway/prisma/schema.prisma                        # 35
grep -Ec "^\s+'[a-z_]+'" database/migrations/02-rls-policies.sql      # 33
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
docker-compose.yml                     # 约 20 个服务（含 profile-gated ollama）
.env.example                           # 环境变量模板
```

---

## 4. 架构特征总结

### 4.1 可复用的现有基础设施

| 组件 | 复用价值 | 原因 |
|---|---|---|
| CloudEvents 1.0 事件信封 | ✅ 高 | 标准化，包含 tenantid + correlationid |
| `BaseAgent` 治理 setter 模式 | ✅ 高 | 可改为 DI 容器以消除硬编码 |
| `providers.py` Provider 边界 | ✅ 高 | 干净抽象，支持新增 Provider；但存在绕过边界的直接 env var 读取 |
| `AgentKillSwitch` (Redis) | ✅ 高 | 4 作用域完整实现 |
| OPA 3 策略并行检查 | ✅ 高 | `agents/core.rego` 可直接扩展 |
| `ApprovalService` (Redis 队列) | ✅ 高 | 人在回路基础设施完整 |
| `ExplainabilityEngine` (PG) | ✅ 高 | 决策记录 Schema 完整 |
| `AgentDecision` / `AgentEvent` / `AgentTask` 表 | ✅ 高 | 可作为 Run Evidence 存储基础 |
| RLS（33 张表） | ✅ 高 | 无需修改 |
| 审计中间件 + audit_logs 表 | ✅ 高 | 无需修改 |
| `HybridRetriever` (结构化+语义+知识) | ✅ 中 | RAG 基础存在 |
| 评测 Runner 框架 | ✅ 中 | 可扩展评测维度 |

### 4.2 需要新增的能力

| 能力 | 当前状态 | 目标 |
|---|---|---|
| Agent 注册表/发现 | ❌ 手动实例化 | `AgentRegistry` 或 DI 容器 |
| 统一 Agent 输出 Schema | ❌ 异构 dict | Pydantic `AgentResponse` |
| AI_MODE 运行边界 | ❌ 仅 AI_PROVIDER 切换 | `AI_MODE` (none/static/supervisor) 控制 Agent 行为模式 |
| Supervisor 路由逻辑 | ❌ 手动 handler | Supervisor Agent + LangGraph StateGraph |
| Specialist 委托模式 | ❌ Router 直接调用 | Specialist 注册 + 能力匹配 |
| Reviewer 验证模式 | ❌ 不存在（ComplianceAgent ≠ Reviewer） | 独立 Reviewer：Evidence Grounding、完整性、冲突和 Action 校验 |
| GovernedExecutor 安全执行 | ❌ 部分（approval 后 emit_event） | 独立受控 Executor，带 approve-gate |
| LangGraph Checkpointer | ❌ 不存在 | PostgresSaver |
| LangGraph Interrupt | ❌ 不存在 | 人在回路中断点 |
| 统一多 Agent Run Trace | ❌ 分散在 OTel/Decision/Event/Task/Audit | 单一 Run/DAG/Task/Handoff/Replan/Token/Cost/Checkpoint Trace |
| Agent 输出质量评测 | ❌ 不存在 | LLM Judge / 对比评测 |
| 多 Agent Trace 评测 | ⚠️ 仅合同评分 | 端到端工作流正确性 |

---

## 5. 风险清单

| # | 风险 | 严重程度 | 描述 |
|---|---|---|---|
| R1 | **循环依赖** | 中 | 若 Supervisor 调用 Specialist 而 Specialist 回调 Supervisor，需在设计层面禁止回环。 |
| R2 | **同步/异步边界** | 中 | Kafka 消费者是异步的，LangGraph StateGraph 需要同步状态传递。 |
| R3 | **Kafka 与 LangGraph 状态一致性** | **高** | 顺序双写（先写 Checkpoint 再写 Outbox）不具备原子性。需设计 Proposal Hash、Idempotency Key、Outbox、执行后验证和必要的事务方案。 |
| R4 | **数据库 Schema 重复** | 低 | 已有 `agent_tasks`、`agent_events`、`agent_decisions` 表。新增 Checkpoint 和 Trace 表时需明确四类存储的不同语义（见 implementation-map.md §存储分类）。 |
| R5 | **Provider 耦合** | 中 | 部分模块绕过 `create_chat_model()` 直接读取 `OLLAMA_*` 环境变量；扩展 Provider 时需先统一入口。 |
| R6 | **测试环境依赖** | 中 | 现有测试依赖 Docker Compose（Postgres、Redis、OPA），新增测试需保持同等隔离级别。 |
| R7 | **前端 API 兼容性** | 低 | `GET /api/v1/agents` 返回字段需保持向后兼容。 |
| R8 | **治理注入冗余** | 中 | 当前 44 行 setter 注入难以维护；Router DI 迁移应作为独立、可回归测试的改动。 |
| R9 | **Agent ID 冲突** | 低 | 新增 Agent 的 `agent_id` 需保持唯一，通过注册表统一管理。 |
| R10 | **OPA 策略盲区** | 中 | OPA 策略仅覆盖 4 个核心 Agent 的能力注册，新增角色需同步更新。 |
| R11 | **无限循环与成本失控** | **高** | Supervisor → Specialist → Reviewer → Replan 循环若无 max_iterations 和 token 预算上限，可能导致无限循环和成本失控。 |
| R12 | **并行竞态与部分失败** | **高** | 多个 Specialist 并行执行时，若部分成功部分失败，Supervisor 需处理部分结果合并、回滚和重试。 |
| R13 | **Tenant Context 与 Foreign Evidence** | **高** | Specialist 检索和 Reviewer 验证时必须强制 tenant 隔离；跨租户证据泄露是安全红线。 |
| R14 | **审批后 Proposal 篡改** | 中 | 审批通过后、Executor 执行前，Proposal 的 payload 若被修改可导致越权操作。需 Proposal Hash 校验。 |
| R15 | **Kafka Replay 和重复副作用** | 中 | Kafka 消息重放时，若 Supervisor Graph 重新执行，可能产生重复的 emit_event 副作用。需 Idempotency Key。 |
| R16 | **Shadow Mode 双执行** | 中 | 迁移阶段可能需要 Shadow Mode（旧路径和新路径并行执行、比对结果但仅旧路径生效），增加复杂度。 |
| R17 | **Checkpoint 数据保留 / GDPR** | 中 | LangGraph Checkpoint 包含用户输入和 Agent 推理，属于 PII 范畴；需与现有 DataGuard 和 GDPR 擦除流程对齐。 |
| R18 | **Prompt / Schema / Registry 版本漂移** | 中 | Specialist 的 system prompt、AgentResponse Schema、AgentRegistry 能力声明三者需要版本同步，否则 Supervisor 可能选择错误的 Specialist。 |
| R19 | **Trace PII 与高基数指标** | 中 | Run Trace 中的 prompt 和 response 内容可能包含 PII；Agent ID × Tenant ID × Task ID 的指标组合可能导致 Prometheus 高基数问题。 |
| R20 | **Reviewer 相关性错误** | 中 | Reviewer 的合同检查必须与 Specialist 的实际产出相关——检查错误的合同条款或遗漏关键约束都会导致 Reviewer 失职。 |
| R21 | **Live Model 非确定性** | 中 | 同一输入、同一 LLM 可能产生不同输出；Supervisor 的意图分类和 Reviewer 的质量判断都可能不稳定。 |
| R22 | **P95 延迟与单位任务成本** | 中 | Supervisor → 多个 Specialist → Reviewer → Executor 的串行链路可能导致单任务延迟远超现有 Kafka handler 的 P95。 |
| R23 | **Agent Capability 与 Tool 权限不一致** | 中 | Specialist 声明的 capability 与其实际可调用的 Tool/API 权限若不一致，可能导致运行时错误或安全问题。 |

---

## 6. 建议的后续实施顺序

基于风险评估和依赖关系，建议调整原有顺序为：

| Phase | 名称 | 核心交付 | 依赖 |
|---|---|---|---|
| **Phase 1** | AI_MODE 和 Provider 运行边界 | `AI_MODE` 环境变量；统一所有 `OLLAMA_*` env var 读取到 Provider 边界；Provider 健康检查 | 无 |
| **Phase 2** | Contracts 和未接线的 Agent Registry | `AgentResponse` Schema；`AgentRegistry`（不替换 Router DI）；`SpecialistCapability` 声明 | Phase 1 |
| **Phase 3** | Complexity Gate、Planner 和 Plan Validator | 请求复杂度评估；Plan 生成器；Plan 结构验证器（确定性规则） | Phase 2 |
| **Phase 4** | 旗舰场景 5 个 Specialist Adapter | Customer Context、Support、Sales、Knowledge、Analytics Adapter | Phase 2 + 3 |
| **Phase 5** | 独立入口的 Supervisor Graph | `POST /api/agents/objectives` 或 `crm.agent.objective.requested` Topic；Supervisor StateGraph | Phase 3 + 4 |
| **Phase 6** | Reviewer 和有限 Replan | Reviewer Agent（Evidence Grounding、完整性、冲突、Action 校验）；最多 1 次 Replan | Phase 5 |
| **Phase 7** | Postgres Checkpointer、Human Approval、GovernedExecutor 和 Verify | PostgresSaver；Interrupt + 审批；GovernedExecutor（approve-gate + verify）；Proposal Hash 校验 | Phase 6 |
| **Phase 8** | Run Trace 和确定性 Demo | 统一 Run/DAG/Task/Handoff/Token/Cost/Checkpoint Trace；可重复的端到端演示 | Phase 7 |
| **Phase 9** | 三组评测 | Single Agent 回归评测；Static Workflow 评测；Supervisor Multi-Agent 端到端评测 | Phase 8 |

**重要说明：**
- Router DI 迁移不作为 Phase 2 的一部分；它是独立、可回归测试的改动，在 Phase 4 之后评估。
- Phase 5 的 Supervisor 入口是**新增独立路径**，不影响现有 `AgentRouter.route()`。
- 固定事件、SLA、审批结果、审计和生命周期更新**继续走现有 Deterministic Kafka Handler 路径**，不迁移。

---

## 7. 尚未解决的开放架构问题

1. **双写原子性** — Checkpoint 写入 PG 和事件发送到 Kafka 无法在同一事务中完成。方案方向：Proposal Hash + Idempotency Key + Outbox 轮询 + 执行后 Verify，但具体实现待设计。
2. **Checkpoint 数据保留与 GDPR** — LangGraph Checkpoint 的 PII 擦除策略与现有 `DataGuard` / GDPR forget 流程的集成方式待定。
3. **Supervisor 入口的 AuthN/AuthZ** — 新增入口的认证授权粒度（Objective 级别的 OPA 策略）待设计。
4. **多 Agent 评测数据集** — Phase 9 的端到端评测用例和 ground truth 标注方案待定。
5. **Feature Flag 粒度** — 开关控制从简单的 `AI_MODE` 逐步演进到 per-tenant per-topic 的细粒度控制。
