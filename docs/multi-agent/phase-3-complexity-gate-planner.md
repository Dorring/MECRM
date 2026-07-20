# Phase 3: Complexity Gate + Planner + Plan Validator

**Status:** Complete (R2)  
**Branch:** `feat/ma-03-complexity-gate-planner`  
**Spec version:** ma-03.2.0

---

## 1. 三条路由的定义

| Route | 语义 | 任务数 | 典型来源 |
|---|---|---|---|
| `deterministic_workflow` | 已有固定 Kafka/Router 工作流处理，不进入 Supervisor | 0 | SLA 超时、审批回调、审计事件、生命周期迁移、Automation Trigger |
| `single_agent` | 单一领域、单一任务类型，一个 Specialist 足够 | 恰好 1 | 单域查询、单域分析 |
| `multi_agent` | 跨领域、多任务类型、冲突信号或 Customer Recovery | ≥ 2 | 客户恢复、跨域分析、冲突信号 |

## 2. 为什么固定 Kafka 工作流不进入 Supervisor

固定 Kafka 事件（`crm.tickets.sla-breached` 等）已在 `AgentRouter` 中绑定到确定性 handler。这些 handler 的行为是：

- **无领域推理**：直接执行 CRUD / 通知 / 状态迁移
- **SLA 敏感**：任何额外跳转都会增加延迟
- **幂等**：Kafka offset 由 Router 管理

如果让 Supervisor 重新规划这些事件，会引入：

1. Planner 延迟（hash 计算 + DAG 验证）
2. 不确定性（即使 Planner 是确定性的，也增加了审计面）
3. 破坏现有 offset / 重试语义

因此 Phase 3 通过 `DETERMINISTIC_EVENT_TYPES` allowlist 在 Complexity Gate 阶段直接返回 `deterministic_workflow`，任务列表为空，Planner 不会为这些事件生成任何 AgentTask。

`KAFKA_TOPIC_TO_EVENT_TYPE` 映射表记录了 Kafka topic → canonical event_type 的对应关系，供上游调用方使用。Phase 3 不修改 Router，不订阅 Kafka。

## 3. Complexity Gate 决策规则

`RuleBasedComplexityGate` 按以下顺序决策（每步只读，无副作用）：

1. **Registry Version 校验** — `request.registry_version != snapshot.version` → `RegistryVersionMismatchError`
2. **最小上下文校验** — `signals.missing_required_context=True` → `InsufficientContextError`
3. **结构性输入矛盾校验** — `requires_cross_domain=True` 但 effective domains < 2、或 `requires_approval=True` 但无 effective task types → `PlanningInputError`
4. **固定事件 allowlist** — `event_type in DETERMINISTIC_EVENT_TYPES` → `deterministic_workflow`
5. **Customer Recovery 模板** — `objective_kind == "customer_recovery"` → `multi_agent`（domains 自动包含 `customer_recovery`）
6. **Multi-agent 触发器** — ≥ 2 effective domains、≥ 2 effective task types、`requires_cross_domain`、`has_conflicting_signals` → `multi_agent`
7. **Single-agent 默认** — 1 effective domain + 0-1 effective task types + 存在 capable agent → `single_agent`

### 3.1 Effective Domains / Task Types（R2 P0-2）

`requested_tasks` 是路由决策的**主要事实来源**。Gate 不再直接读取 `signals.domains` / `signals.requested_task_types`，而是通过两个纯函数派生：

```python
effective_domains(signals) =
    {t.domain for t in signals.requested_tasks}
    if signals.requested_tasks else signals.domains

effective_task_types(signals) =
    {t.task_type for t in signals.requested_tasks}
    if signals.requested_tasks else signals.requested_task_types
```

规则：

- 只提供 `requested_tasks` 也能正常决策（不需要同时填 `domains` / `requested_task_types`）
- 同时提供时，派生集合必须与显式集合完全一致（由 Pydantic 校验保证）
- Customer Recovery 路由在调用方未提供 domains 时，自动使用 `["customer_recovery"]`

**两种冲突信号语义**（修正 4）：

- **可分析的业务冲突**（如支持满意度低 vs 销售续约概率高）：上下文完整，仅业务结论冲突 → `multi_agent` + `reason=conflicting_signals`
- **结构性输入矛盾**（如 `requires_cross_domain=True` 但只有一个 domain）：请求本身不可规划 → `PlanningInputError`

## 4. Planner 与 AgentRegistry 的关系

`DeterministicPlanner` 通过 4 个只读 API 与 Registry 交互：

| API | 用途 | 副作用 |
|---|---|---|
| `registry.snapshot()` | 获取 version + 全部 AgentCapability 副本 | 无（深拷贝） |
| `registry.list_all()` | 遍历候选 agent | 无（深拷贝） |
| `registry.is_registered(agent_id)` | 存在性检查 | 无 |
| `registry.tool_catalog.is_registered(name)` / `resolve(name)` | 工具存在性 + 权限 | 无 |

Planner **不调用** `registry.resolve()`（返回 handler 引用）、`registry.register()` / `replace()` / `unregister()`。

## 5. Expected Intents — 共享纯函数（R2 P0-1）

`resolve_expected_intents(request, decision)` 是 Planner 和 Validator 共同使用的**单一 Intent 来源**。它确保 Planner 生成的计划内容确实来自原始请求，防止"替换成另一个 Registry 支持的任务后重新计算 Hash 即通过"。

路由规则：

| Route | Intent 来源 |
|---|---|
| `deterministic_workflow` | 空列表 |
| `single_agent` | `requested_tasks[0]` 或从 signals 合成；**多个 RequestedTask 报错** |
| `multi_agent` + `customer_recovery` | Customer Recovery 模板（5 个 TaskIntent） |
| `multi_agent` 其他 | 每个 `RequestedTask` 转换为一个 `TaskIntent`；缺失时报错 |

## 6. 最小权限 + Tool-aware Agent 选择

### 6.1 候选筛选（全部为 AND）

1. `enabled=True`
2. `supported_tasks` 包含 `intent.task_type`
3. `domains` 包含 `intent.domain`
4. `authority` 是 `READ` 或 `PROPOSE`（**EXECUTE 被过滤**，不是立即失败）
5. `authority >= intent.preferred_authority`
6. **`required_tools ⊆ cap.allowed_tools`**（R2 P0-3）
7. **每个 required tool 必须存在于 ToolCatalog**（R2 P0-3）
8. **每个 required tool 的 authority ≤ cap.authority**（R2 P0-3）
9. **每个 required tool 的 authority ≤ PROPOSE**（Phase 3 上限，R2 P0-3）

### 6.2 排序键（升序，确定性）

1. `_AUTHORITY_RANK`：READ(0) < PROPOSE(1) < EXECUTE(2)
2. `_COST_CLASS_RANK`：low(0) < medium(1) < high(2)
3. `timeout_ms`：更小优先
4. `agent_id`：字典序
5. `version`：字典序

**EXECUTE 过滤策略**（修正 3）：EXECUTE agent 从候选集中排除。如果排除后仍有 READ/PROPOSE 候选，正常选择最小权限。如果只剩 EXECUTE 候选，Fail-Closed。

**Tool-aware 筛选**（R2 P0-3）：候选列表在排序前先过滤掉不满足 `required_tools` 的 agent。即使存在更便宜的 agent，如果它缺少必需工具，Planner 也会选择更贵但具备工具能力的 agent。

## 7. Multi-Agent 全局确定性分配（R2 P0-4）

multi_agent 路由不能逐任务独立选择后再依赖 Validator 拒绝同一 agent。Planner 必须保证 ≥2 个不同 Agent 的可行组合。

### 7.1 算法

1. 为每个 Intent 建立完整候选列表（已稳定排序）
2. 笛卡尔积搜索所有可行组合
3. 丢弃 distinct agent 数 < 2 的组合
4. 按以下复合键选择最优组合（升序）：
   - 总 authority rank（最小权限优先）
   - 总 cost class rank（最低成本优先）
   - 总 timeout_ms
   - agent_id 拼接（字典序）
   - version 拼接（字典序）
5. 无可行组合时，回退到逐任务贪心选择，由 Validator 报 `multi_agent_too_few_agents`

### 7.2 复杂度

Phase 3 任务数和候选数受 `max_tasks` 约束，笛卡尔积规模可控。第一版不实现复杂优化器。

## 8. Customer Recovery Plan 示例

模板 `CustomerRecoveryTemplate` 生成 5 个 TaskIntent：

```
customer_context          (required=True,  READ, crm_reader.get_customers)
    ├── support_analysis          (required=True,  READ, crm_reader.get_tickets)
    ├── sales_risk_analysis       (required=True,  READ, crm_reader.get_deals)
    ├── knowledge_recommendation  (required=False, READ, vector_search.search)
    └── recovery_metrics          (required=False, READ, crm_reader.get_customers)
```

五个任务全部生成。`required=False` 仅为后续降级执行提供语义，Phase 3 Planner 不会省略任何任务。

Customer Recovery 路由在调用方未提供 domains 时，自动使用 `["customer_recovery"]`（R2 P1-A），确保 ComplexityDecision.domains 与模板 Domain 一致。

## 9. Plan DAG

DAG 通过 `AgentTask.dependencies`（`frozenset[str]`）表达。Planner 在构造 AgentTask 前将 `TaskIntent.dependencies`（intent_id 空间）映射为 `task_id` 空间。

Validator 校验：

- 无自依赖
- 无重复依赖
- 无缺失依赖
- 无环（Kahn's 算法）
- Required task 不得依赖 Optional task
- 可生成稳定拓扑顺序

## 10. Plan Hash + Intent Binding

### 10.1 两层 Hash 设计

```
request_hash = SHA-256(run_id, tenant_id, actor, objective, signals, budget, registry_version)

plan_hash    = SHA-256(request_hash, complexity, canonical_tasks, planner_version)
```

**canonical_tasks** 排序后序列化，排除 volatile 字段（`created_at` / `started_at` / `completed_at`）。

**排除字段**：`summary`、`warnings`、wall-clock time、`plan_hash` 本身。

**Hash 不变量**：

- 同输入 + 同 Registry → 同 `plan_hash`
- 伪造 `plan_hash` → `PlanDraft` 构造时抛 `ValidationError`
- 原地修改 task 后 `verify_integrity()` 抛 `PlanIntegrityError`
- Task 列表顺序变化不改变 `plan_hash`（按 `task_id` 排序后 hash）

### 10.2 Intent Binding 逐字段校验（R2 P0-1）

Hash 能证明"计划没被偷偷修改"，但**不能**证明"计划内容确实来自原始请求"。因此 Validator 通过 `resolve_expected_intents` 重算 Expected Intents，并对每个 `PlannedTask` 逐字段比较：

| 字段 | 校验规则 |
|---|---|
| `intent_id` | 必须存在于 Expected Intents；不得重复 |
| `domain` | `== Expected Intent.domain` |
| `task_type` | `== Expected Intent.task_type` |
| `objective` | `== Expected Intent.objective` |
| `preferred_authority` | `== Expected Intent.preferred_authority` |
| `required_tools` | `== Expected Intent.required_tools` |
| `estimated_tool_calls` | `== Expected Intent.estimated_tool_calls` |
| `required` | `== Expected Intent.required` **且** `== AgentTask.required` |
| `task_id` | `== stable_hash({run_id, intent_id, task_type, agent_id})[:24]` |
| `idempotency_key` | `== f"{run_id}:{task_id}"` |

任何不一致返回稳定 Issue Code：

| Issue Code | 触发条件 |
|---|---|
| `plan_intent_mismatch` | PlannedTask 字段与 Expected Intent 不一致 |
| `unstable_task_id` | task_id 不符合稳定算法 |
| `idempotency_key_mismatch` | idempotency_key 不符合 `run_id:task_id` |
| `planned_task_required_mismatch` | PlannedTask.required != AgentTask.required |
| `duplicate_intent_id` | 多个 PlannedTask 使用同一 intent_id |

## 11. Budget 校验

### 11.1 估算规则（修正 7）

| 估算项 | 公式 |
|---|---|
| `estimated_agent_calls` | `len(tasks)` |
| `estimated_tool_calls` | `sum(pt.estimated_tool_calls)` |
| `estimated_iterations` | DAG 最长路径节点数 |
| `estimated_deadline_ms` | DAG 最长路径上 `task.timeout_ms` 之和 |

### 11.2 硬性 Fail-Closed（结构预算）

- `max_tasks`
- `max_agent_calls`
- `max_tool_calls`
- `max_iterations`
- `deadline_ms`

### 11.3 Tool Budget 低报防护（R2 P0-5）

`RequestedTask` 和 `TaskIntent` 在 Pydantic model_validator 中强制：

```python
if required_tools and estimated_tool_calls < len(required_tools):
    raise ValueError(...)
```

不允许"需要工具但预计零次调用"的任务通过 Contract 校验。

### 11.4 软性 warning（无可靠估算）

- `token_budget` 已设置但 Phase 3 无 token 估算 → `estimate_unavailable` warning
- `cost_budget_usd` 已设置但 Phase 3 无美元估算 → `estimate_unavailable` warning

不伪造 token 数或美元金额。

## 12. Fail-Closed 行为

| 场景 | 错误类型 | Issue Code |
|---|---|---|
| Registry version 不匹配 | `RegistryVersionMismatchError` | `registry_version_mismatch` |
| 缺少必要上下文 | `InsufficientContextError` | — |
| 结构性输入矛盾 | `PlanningInputError` | — |
| 无 capable agent（含 EXECUTE-only） | `UnsupportedCapabilityError` | — |
| 预算超限 | `BudgetExceededPlanningError` / `PlanValidationError` | `*_budget_exceeded` / `deadline_exceeded` |
| Plan hash 不匹配 | `PlanIntegrityError` / `ValidationError` | `plan_hash_mismatch` / `request_hash_mismatch` |
| DAG 有环 | `PlanCycleError` | `cycle` |
| Intent 绑定不一致 | `PlanValidationError` | `plan_intent_mismatch` / `unstable_task_id` / `idempotency_key_mismatch` / `planned_task_required_mismatch` / `duplicate_intent_id` |
| Multi-agent 不足两个 Agent | `PlanValidationError` | `multi_agent_too_few_agents` |

### 12.1 异常处理收紧（R2 P1-B）

Validator 重算 Gate 时**只捕获 `PlanningError`**，不捕获 `Exception`。未知编程错误（如 `RuntimeError`）必须正常暴露到测试、日志和错误监控，不得被静默降级为 Validation Issue。

所有错误使用稳定 `code` 字段，不暴露内部异常、API key、endpoint 或 chain-of-thought。

## 13. 本阶段为何不执行 Agent

Phase 3 只负责**生成和验证计划**，不执行任何业务副作用：

- 不实现 Supervisor / LangGraph 执行图
- 不调用真实 Specialist handler
- 不修改 `AgentRouter`
- 不写入 CRM
- 不调用 GovernedExecutor
- 不修改审批 / OPA / RLS
- 不修改数据库 Schema
- 不接入真实外部 LLM 网络
- 不默认启用 Ollama
- 不保存 Chain-of-thought

执行留待 Phase 4+（Supervisor Graph）接入。

## 14. Phase 4/5 的接入点

| 接入点 | 位置 | 用途 |
|---|---|---|
| `PlanDraft.agent_tasks()` | `planning.py` | 提取 `list[AgentTask]` 供 Supervisor 派发 |
| `PlanValidationReport.topological_order` | `planning.py` | Supervisor 按拓扑序调度 |
| `PlanValidationReport.estimated_*` | `planning.py` | 运行时预算追踪基线 |
| `PlanDraft.verify_integrity()` | `planning.py` | Checkpoint 恢复后重验 |
| `PlanningRequest.budget` | `planning.py` | 运行时预算消耗累计 |

Phase 4 Supervisor 将：

1. 接收 `PlanDraft`
2. 调用 `agent_tasks()` 获取任务列表
3. 按 `topological_order` 调度
4. 每个 task 完成后更新 `ExecutionUsage`
5. 预算耗尽时 Fail-Closed

## 15. 已知限制

1. **无 LLM Gate** — Phase 3 只有 `RuleBasedComplexityGate`，不实现依赖网络的 LLM Gate
2. **无 Token/Cost 估算** — `token_budget` 和 `cost_budget_usd` 只产生 warning，不产生估算值
3. **单模板** — 只有 Customer Recovery 模板；其他 multi-agent 场景从 `RequestedTask` 显式映射 intents（R2 更新）
4. **不执行** — Plan 生成后不执行任何 agent handler
5. **不改 Router** — 现有 Kafka/Router 工作流完全不受影响
6. **Authority 上限** — Phase 3 最高 `PROPOSE`，不选择 `EXECUTE` agent
7. **Multi-agent 分配** — 第一版使用笛卡尔积搜索，受 `max_tasks` 约束；不实现复杂优化器

---

## 测试覆盖

| 文件 | 测试数 | 覆盖范围 |
|---|---|---|
| `test_complexity_gate.py` | 17 | 三路由、Fail-Closed、结构矛盾、确定性、无 CoT |
| `test_planner.py` | 17 | 空/单/多计划、最小权限、cost/id tiebreaker、稳定 ID/Hash、无副作用、无网络 |
| `test_plan_validator.py` | 25 | DAG、路由约束、Registry/Tool 权限、5 类预算、Hash 篡改、顺序无关 |
| `test_planning_templates.py` | 16 | Customer Recovery 5 任务计划、拓扑序、Fail-Closed 场景 |
| `test_phase3_r1_review.py` | 29 | R1 反例：request_hash 绑定、Gate 重算、RequestedTask 映射、Authority 层级、依赖 Fail-Closed、Kafka 映射、错误类型映射 |
| `test_phase3_r2_review.py` | 26 | R2 反例：Intent Binding、requested_tasks 真值来源、Tool-aware 选择、Multi-agent 全局分配、Tool Budget 低报、Customer Recovery Domain、Gate 异常处理 |
| **Phase 3 合计** | **130** | |
| Phase 2 回归 | 168 | 全部通过 |
| Phase 1 回归 | 76 | 全部通过 |
| **总计** | **374** | |

## 新增文件

```
agents/src/multi_agent/
├── planning_errors.py          # 10 个错误类型
├── planning.py                 # 6 个 Contract + hash 计算 + effective_domains/task_types + resolve_expected_intents
├── complexity_gate.py          # RuleBasedComplexityGate (使用 effective sets)
├── planning_templates.py       # CustomerRecoveryTemplate
├── plan_validator.py           # PlanValidator (含 Intent Binding 校验)
└── planner.py                  # DeterministicPlanner (含 tool-aware 选择 + 全局分配)

agents/tests/unit/multi_agent/
├── test_complexity_gate.py
├── test_planner.py
├── test_plan_validator.py
├── test_planning_templates.py
├── test_phase3_r1_review.py    # R1 反例测试
└── test_phase3_r2_review.py    # R2 反例测试

docs/multi-agent/
└── phase-3-complexity-gate-planner.md  (本文件)
```

## 修改文件

- `agents/src/multi_agent/__init__.py` — 追加 Phase 3 导出 + `effective_domains` / `effective_task_types` / `resolve_expected_intents`
