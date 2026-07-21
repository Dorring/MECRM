# Phase 3: Complexity Gate + Planner + Plan Validator

**Status:** Complete (R7)  
**Branch:** `feat/ma-03-complexity-gate-planner`  
**Spec version:** ma-03.7.0

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
5. **Customer Recovery 模板** — `objective_kind == "customer_recovery"` → `multi_agent`（domains 强制为 `["customer_recovery"]`，模板输入互斥校验见 §8.1）
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
- Customer Recovery 路由**强制**使用 `["customer_recovery"]`（R3 P0-4 收紧），并执行模板输入互斥校验（§8.1）

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

## 7. Multi-Agent 全局确定性分配（R2 P0-4 + R3 P0-2/P1 + R4 P0-2）

multi_agent 路由不能逐任务独立选择后再依赖 Validator 拒绝同一 agent。Planner 必须保证 ≥2 个不同 Agent 的可行组合。R3 起，该算法抽取为共享纯函数 `resolve_agent_assignment(request, decision, intents, registry)`，Planner 与 Validator 共同使用（§16）。R4 起算法升级为 **budget-aware**：结构性预算预检 + 每组合 DAG 关键路径 deadline 过滤，确保 Planner 选择的是"预算可行组合中的确定性最优"，而不是"全局最优但被 Validator 拒绝"。

### 7.1 预检（R3 P1 + R4 P0-2）

搜索开始前先检查全部结构性预算，避免在已知不可行时浪费 CPU：

- `len(intents) > budget.max_tasks` → `BudgetExceededPlanningError`
- `len(intents) > budget.max_agent_calls` → `BudgetExceededPlanningError`
- `sum(intent.estimated_tool_calls) > budget.max_tool_calls` → `BudgetExceededPlanningError`（R4 P0-2 新增）
- `_longest_path_node_count(intents) > budget.max_iterations` → `BudgetExceededPlanningError`（R4 P0-2 新增）

### 7.2 算法

1. 为每个 Intent 调用 `resolve_candidate_agents(intent, registry)` 建立候选列表（已稳定排序，含 Tool-aware 过滤）
2. 任一 Intent 无候选 → `UnsupportedCapabilityError`
3. `single_agent` 路由或 `len(intents) < 2` → 按 `timeout_ms <= budget.deadline_ms` 过滤候选，取首位；无 feasible 候选 → `BudgetExceededPlanningError`
4. `multi_agent` 路由 + `len(intents) >= 2`：
   1. 笛卡尔积搜索所有可行组合
   2. **搜索空间上限**：`total_combinations > MAX_ASSIGNMENT_COMBINATIONS`（默认 1,000,000）→ `UnsupportedCapabilityError` Fail-Closed（R3 P1）
   3. 丢弃 distinct agent 数 < 2 的组合
   4. **R4 P0-2 — DAG 关键路径 deadline 过滤**：对每个剩余组合计算 `_estimate_assignment_deadline_ms(intents, combo_assignment)`（DAG 最长路径上 `timeout_ms` 之和），丢弃 `combo_deadline > budget.deadline_ms` 的组合
   5. 按以下复合键对**预算可行组合**排序（升序）：
      - 总 authority rank（最小权限优先）
      - 总 cost class rank（最低成本优先）
      - 总 timeout_ms
      - agent_id 拼接（字典序）
      - version 拼接（字典序）
   6. **无可行 diverse 组合** → `UnsupportedCapabilityError`（R3 P1，不回退贪心）
   7. **有 diverse 组合但无预算可行组合** → `BudgetExceededPlanningError`（R4 P0-2 新增）

### 7.3 复杂度

`MAX_ASSIGNMENT_COMBINATIONS` 是硬上限。第一版不实现 Branch-and-Bound 或动态规划优化器，由上限保证 CPU 安全。R4 的 deadline 过滤是 `O(combos × (V+E))`，其中 `V+E` 是 Intent DAG 的节点数 + 边数，被 `MAX_ASSIGNMENT_COMBINATIONS` 间接限定。

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

Customer Recovery 路由**强制**使用 `["customer_recovery"]`（R3 P0-4 收紧），并通过 `_validate_customer_recovery_exclusivity()` 执行模板输入互斥校验，确保 `ComplexityDecision.domains` == Expected Intent domains == PlannedTask domains。

### 8.1 模板输入互斥性（R3 P0-4）

当 `objective_kind == "customer_recovery"` 时，模板独占生成 domains / task_types / tasks。调用方提供的显式信号必须为空或与模板完全一致：

| 字段 | 允许值 |
|---|---|
| `signals.requested_tasks` | 必须为空 |
| `signals.domains` | 空或 `== {"customer_recovery"}` |
| `signals.requested_task_types` | 空或 `== 模板 5 个 task_type 的集合` |

违反任一规则 → `PlanningInputError`，不静默忽略显式信号。这防止了"Gate 读取显式信号派生 Domain，随后 `resolve_expected_intents()` 又使用固定模板"的不一致路径。

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

### 10.2 Intent Binding 逐字段校验（R2 P0-1，R3 升级为 Canonical Plan Reconstruction）

Hash 能证明"计划没被偷偷修改"，但**不能**证明"计划内容确实来自原始请求"。R2 通过 `resolve_expected_intents` 重算 Expected Intents 并逐字段比较。

**R3 升级**：R2 的 Intent Binding 只比较"Plan 内容自洽"的字段，仍然允许调用方替换 Agent、降低 timeout、删除依赖后重新计算 `plan_hash` 通过验证。R3 将 Validator 升级为**完整重建 Canonical Plan**（§10.3），覆盖原 Intent Binding 的全部字段，并新增 Dependency / Required Evidence / Capability-derived / Plan-time Lifecycle / Planner Version 校验。

R2 原有的字段比较仍保留，作为 Canonical Plan Reconstruction 的子集：

| 字段 | 校验规则 | Issue Code |
|---|---|---|
| `intent_id` | 必须存在于 Expected Intents；不得重复 | `plan_intent_mismatch` / `duplicate_intent_id` |
| `domain` | `== Expected Intent.domain` | `plan_intent_mismatch` |
| `task_type` | `== Expected Intent.task_type` | `plan_intent_mismatch` |
| `objective` | `== Expected Intent.objective` | `plan_intent_mismatch` |
| `preferred_authority` | `== Expected Intent.preferred_authority` | `plan_intent_mismatch` |
| `required_tools` | `== Expected Intent.required_tools` | `plan_intent_mismatch` |
| `estimated_tool_calls` | `== Expected Intent.estimated_tool_calls` | `plan_intent_mismatch` |
| `required` | `== Expected Intent.required` **且** `== AgentTask.required` | `planned_task_required_mismatch` |
| `task_id` | `== stable_hash({run_id, intent_id, task_type, agent_id})[:24]` | `unstable_task_id` |
| `idempotency_key` | `== f"{run_id}:{task_id}"` | `idempotency_key_mismatch` |

### 10.3 Canonical Plan Reconstruction（R3 P0-1/P0-2/P0-3）

R3 起，Validator 不再只做部分字段检查，而是从 `(request, registry)` 完整重建预期 Canonical Plan 并逐字段比较。重建流程：

```
1. resolve_expected_intents(request, plan.complexity)         # 共享纯函数
2. resolve_agent_assignment(request, plan.complexity,
                             expected_intents, registry)      # 共享纯函数
3. build_expected_planned_tasks(request, expected_intents,
                                expected_assignment)          # 共享纯函数
4. 对每个 PlannedTask 逐字段比较 (§10.4)
```

Validator **不得**调用 `DeterministicPlanner.create_plan()`，否则会产生递归。三个共享纯函数位于 `multi_agent.planning` 模块（§16）。

### 10.4 Canonical AgentTask 字段比较（R3 P0-1/P0-2/P0-3 + R4 P1-1/P1-3）

Canonical Plan 重建后，Validator 对每个 `PlannedTask` 及其内部 `AgentTask` 逐字段比较。所有字段必须**完全相等**（不是 `<=` 或子集）：

| 字段组 | 字段 | 校验规则 | Issue Code |
|---|---|---|---|
| Intent-level | domain / task_type / objective / preferred_authority / required_tools / estimated_tool_calls / required | `== Expected Intent` 对应字段 | `plan_intent_mismatch` |
| Agent assignment | `agent_id` | `== Expected Capability.agent_id` | `agent_assignment_mismatch` |
| Task identity | `task_id` | `== _stable_task_id(run_id, intent_id, task_type, agent_id)` | `unstable_task_id` |
| Task identity | `idempotency_key` | `== f"{run_id}:{task_id}"` | `idempotency_key_mismatch` |
| **Dependencies**（R3 P0-1） | `dependencies` | `== frozenset(expected_task_id_by_intent[dep] for dep in intent.dependencies)` | `dependency_mismatch` |
| **Required Evidence**（R3 P0-1） | `required_evidence` | `== Expected Intent.required_evidence`（列表相等） | `required_evidence_mismatch` |
| **Capability-derived**（R3 P0-3） | `timeout_ms` | `== Expected Capability.timeout_ms`（**不可降低**） | `task_field_mismatch` |
| **Plan-time Lifecycle**（R3 P0-3） | `status` | `== "pending"` | `task_lifecycle_violation` |
| **Plan-time Lifecycle**（R3 P0-3） | `started_at` | `is None` | `task_lifecycle_violation` |
| **Plan-time Lifecycle**（R3 P0-3） | `completed_at` | `is None` | `task_lifecycle_violation` |
| **Fixed Canonical**（R3 P0-3） | `max_retries` | `== 0`（**R5 P0-1 起由 `RetryPolicy.max_retries` 替代**，详见 §10.6） | `task_field_mismatch` |
| **Fixed Canonical**（R3 P0-3） | `priority` | `== "medium"` | `task_field_mismatch` |
| **Fixed Canonical**（R3 P0-3） | `input_data` | `== {}` | `task_field_mismatch` |
| **Fixed Canonical**（R3 P0-3） | `user_id` | `is None` | `task_field_mismatch` |
| **Fixed Canonical**（R3 P0-3） | `correlation_id` | `is None` | `task_field_mismatch` |
| **Planning Metadata**（R4 P1-1） | `planning_metadata` | `== Expected PlannedTask.planning_metadata`（dict 相等，从 `TaskIntent.metadata` 原样复制） | `task_field_mismatch` |

`created_at` 不参与 Hash 和语义比较（允许 wall-clock 时间）。

**R4 P1-3 — Agent Version 校验移除**：`PlannedTask` 不携带 `agent_version` 字段，原 `agent_version_mismatch` Issue Code 已删除。Version drift 由 plan-level `registry_version` 检查覆盖（整个 Capability 集合绑定到单个 Registry Snapshot）。

### 10.5 Dependency 语义绑定（R3 P0-1）

Validator 建立 `expected_task_id_by_intent` 映射，将 Intent 空间的依赖转换为 task_id 空间后严格比较：

```python
expected_task_id_by_intent = {
    intent.intent_id: _stable_task_id(
        run_id=request.run_id,
        intent_id=intent.intent_id,
        task_type=intent.task_type,
        agent_id=expected_assignment[intent.intent_id].agent_id,
    )
}

expected_dependencies = frozenset(
    expected_task_id_by_intent[dep]
    for dep in intent.dependencies
)

# AgentTask.dependencies 必须完全等于 expected_dependencies
assert actual_task.dependencies == expected_dependencies
```

这防止了"删除 Customer Recovery 四条根依赖后重新计算 `plan_hash` 通过验证"的攻击。

### 10.6 Planner Version 校验（R3 P0-5）

`planner_version` 被包含在 `plan_hash` 中，但 R2 Validator 不检查它是否等于当前支持的版本。R3 新增校验：

```python
if plan.planner_version != PLANNER_VERSION:
    issue(code="planner_version_mismatch", severity="error")
```

当前 `PLANNER_VERSION = "ma-03.7.0"`。未来需要支持旧版本时，应使用显式版本 Registry `SUPPORTED_PLANNER_VERSIONS`，不接受任意非空字符串。

### 10.7 Intent Graph 校验（R4 P0-1）

R3 之前，Intent 图校验（`intent_id` 唯一、依赖存在、无环）只是 Planner 的私有逻辑：Planner 在 Agent Assignment 前调用 `_validate_intents()`，但 Validator 直接调用 `build_expected_planned_tasks()`，后者执行 `intent_to_task_id[dep]`。当 Request 含不存在的依赖时，Validator 会直接抛出 `KeyError` 而不是返回稳定的 Validation Issue。

R4 将该校验抽取为共享纯函数 `validate_intent_graph(intents) -> list[str]`，Planner 和 Validator 共同调用，返回稳定的 Issue Code 列表：

| Issue Code | 触发条件 |
|---|---|
| `duplicate_intent_id` | 两个 Intent 拥有相同的 `intent_id` |
| `missing_intent_dependency` | Intent 的 `dependencies` 引用了不存在的 `intent_id` |
| `intent_cycle` | Intent 依赖图存在环（仅在没有 missing dependency 时检测，避免误报） |

Validator 在 `_check_canonical_plan` 的 Step 1b 调用该函数；若返回非空列表，立即短路返回稳定 Issue，不再进入 Agent Assignment / Canonical Task 构造阶段。这关闭了"非法 Request 让 Validator 崩溃"的 Fail-Closed 边界漏洞。

### 10.8 Intent / Tool Authority 对齐（R4 P0-3）

R3 之前，`resolve_candidate_agents()` 保证 Agent 有权使用 Required Tool（READ agent 不能用 PROPOSE tool），但没有验证 `TaskIntent.preferred_authority` 本身是否覆盖 Required Tool 的 Authority。攻击者可以提交 `preferred_authority=READ` + `required_tools={"crm_writer.propose"}`，Planner 会选择一个 PROPOSE Agent，Validator 也返回 `valid=True`，但 PlannedTask 仍被标记为 `preferred_authority=READ` —— 后续 Supervisor 会看到一个"READ Task"但实际被授权使用 PROPOSE tool。

R4 新增共享纯函数 `validate_intent_tool_authority(intent, registry)`，在 Intent 解析后、Agent Assignment 前调用。规则：

```
TOOL_TO_AGENT_AUTHORITY = MappingProxyType({
    ToolAuthority.READ    → AgentAuthority.READ
    ToolAuthority.PROPOSE → AgentAuthority.PROPOSE
    ToolAuthority.EXECUTE → AgentAuthority.EXECUTE  # Phase 3 直接拒绝
})  # R6 P0-4 — 不可变 Mapping，__setitem__/__delitem__ 抛 TypeError

required_authority = max(
    TOOL_TO_AGENT_AUTHORITY[tool.authority]
    for tool in intent.required_tools
)

if intent.preferred_authority < required_authority:
    raise PlanningInputError(...)
```

**禁止静默提升 Preferred Authority** —— 调用方明确声明了权限边界，Validator 不得在 Planner 不知情的情况下修改它。违反时以 `PlanningInputError` Fail-Closed。Validator 在 `_check_canonical_plan` 的 Step 1c 调用同一函数；违反时返回稳定 Issue Code `tool_authority_mismatch`。

**R6 P0-4 — Mapping 不可变**：`TOOL_TO_AGENT_AUTHORITY` 使用 `MappingProxyType` 包装，外部代码无法通过 `TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] = AgentAuthority.READ` 降级权限边界。详见 §10.15。

### 10.9 Write / Approval Requirement 校验（R5 P0-1）

R4 之前，"如果 `signals.requires_write` 或 `signals.requires_approval` 为 True，则至少一个 Intent 必须有 `preferred_authority == PROPOSE`" 这条规则只存在于 `DeterministicPlanner._validate_write_approval_requirements()` 私有方法中。Validator 的 Canonical Reconstruction 没有调用同一规则，因此绕过 `create_plan()`、直接手工构造 `PlanDraft` 的请求可以让 "要求写入" 的请求只包含 READ Task，并仍通过校验。

R5 将该规则抽取为共享纯函数 `validate_write_approval_requirements(request, intents) -> list[str]`，Planner 和 Validator 共同调用。Validator 在 `_check_canonical_plan` 的 Step 1d 调用，返回稳定 Issue Code：

| Issue Code | 触发条件 |
|---|---|
| `write_request_missing_propose_intent` | `requires_write=True` 但没有 Intent 的 `preferred_authority == PROPOSE` |
| `approval_request_missing_propose_intent` | `requires_approval=True` 但没有 Intent 的 `preferred_authority == PROPOSE` |

Planner 仍在 `create_plan()` 中以 `PlanningInputError` Fail-Closed；Validator 返回稳定 Issue 而不是抛异常。

### 10.10 Immutable Request Snapshot + Execution Task Defensive Copy（R5 P0-2）

`PlanDraft` 文档声明保存完整 `PlanningRequest` 快照，但 Pydantic v2 在调用方传入预构建模型时会复用同一嵌套实例，导致 `plan.request is original_request` 成立。外部修改原始请求（或其嵌套 `PlanningSignals` / `RequestedTask`）会同步破坏 PlanDraft 并使 `request_hash` / `plan_hash` 失效。

R5 在 `PlanDraft` 的 `field_validator("request")` 中强制深拷贝：

```python
@field_validator("request")
@classmethod
def _request_deep_snapshot(cls, v: PlanningRequest) -> PlanningRequest:
    return PlanningRequest.model_validate(v.model_dump(mode="python"))
```

边界由 PlanDraft Contract 自身强制，而非由 Planner 在调用点复制 —— 手工构造 PlanDraft 也无法绕过。

类似地，原 `PlanDraft.agent_tasks()` 直接返回内部 `AgentTask` 引用（`plan.tasks[i].task`），外部修改返回任务的 `status` / `started_at` 会破坏 PlanDraft。R5 将其重命名为 `build_execution_tasks()` 并返回深拷贝：

```python
def build_execution_tasks(self) -> list[AgentTask]:
    return [
        AgentTask.model_validate(pt.task.model_dump(mode="python"))
        for pt in self.tasks
    ]
```

重命名同时让调用方明确这是一次有代价的"从不可变 Plan 生成新执行任务"操作，而非一个内部视图。

### 10.11 Semantic Request Hash（R5 P0-3）

R4 之前，`compute_request_hash()` 直接对 `request.signals.model_dump(mode="json")` 进行 Canonicalize，而 Canonicalizer 保留 List 顺序。因此：

* 交换两个独立 `RequestedTask` 的列表顺序会改变 `request_hash`（进而改变 `plan_hash`）；
* 某个 Intent 的 `dependencies = ["a", "b"]` 与 `["b", "a"]` 会产生不同 hash，尽管 Dependencies 的语义是集合，最终 `AgentTask.dependencies` 也是 `frozenset`。

这与 Phase 3 的目标不一致 —— 任务顺序不代表依赖；依赖关系由 DAG 显式表达。

R5 新增 `canonical_request_payload(request) -> dict[str, Any]` 函数，规范化以下字段的顺序：

| 字段 | 规范化规则 |
|---|---|
| `signals.requested_tasks` | 按 `intent_id` 排序 |
| 每个 `RequestedTask.dependencies` | 排序 |
| `signals.domains` / `signals.requested_task_types` | 排序（已为 `frozenset`，但 `model_dump` 输出 list） |
| 每个 `RequestedTask.required_tools` | 排序 |

`compute_request_hash()` 改为 `stable_hash(canonical_request_payload(request))`。

不变量：

```
同一 Intent DAG，仅输入列表顺序不同
→ 相同 request_hash
→ 相同 plan_hash

依赖目标或任务语义真正变化
→ Hash 必须变化
```

### 10.12 Cross-process Hash Stability（R6 P0-1）

R5 之前，共享 Canonicalizer 和 Registry Snapshot 在序列化 BaseModel 时使用 `model_dump(mode="json")`。该模式将 `frozenset` 字段（`AgentTask.dependencies`、`PlannedTask.required_tools`、`AgentCapability.domains` / `supported_tasks` / `allowed_tools`）转换为普通 `list`。由于 `frozenset` 的迭代顺序受 `PYTHONHASHSEED` 影响，转换后的 `list` 保留了进程随机的顺序。而 Canonicalizer 的 set/frozenset 分支会排序、list 分支保留原始顺序，导致同一份 Plan 在不同 `PYTHONHASHSEED` 的进程中产生不同的 `plan_hash` 和 `RegistrySnapshot.version`。

R6 修复策略 — 全链路使用 `mode="python"`：

| 位置 | 修改前 | 修改后 |
|---|---|---|
| `serialization._canonical_value` BaseModel 分支 | `model_dump(mode="json")` | `model_dump(mode="python")` |
| `serialization.stable_hash` | `model_dump(mode="json")` | `model_dump(mode="python")` |
| `planning._canonical_tasks_payload` | `model_dump(mode="json")` | `model_dump(mode="python")` |
| `planning.canonical_request_payload` | `model_dump(mode="json")` | `model_dump(mode="python")` |
| `planning.compute_plan_hash` | `model_dump(mode="json")` | `model_dump(mode="python")` |
| `registry._copy_capability` | `model_dump(mode="json")` | `model_dump(mode="python")` |
| `registry.snapshot` | `model_dump(mode="json")` | `model_dump(mode="python")` |

`mode="python"` 保留 `frozenset` 类型，让 Canonicalizer 的 set/frozenset 分支排序，而不是看到 list 后保留进程随机顺序。

**`ComplexityDecision.domains` / `.reasons` 显式排序**：这两个字段类型为 `list[str]`（不是 `frozenset`），但当 `DeterministicPlanner` 传入 `frozenset({"support", "sales"})` 时，Pydantic 按 `frozenset` 迭代顺序构造 `list`，迭代顺序受 `PYTHONHASHSEED` 影响。`compute_plan_hash` 在调用 Canonicalizer 前显式排序：

```python
complexity_data = complexity.model_dump(mode="python")
if isinstance(complexity_data.get("domains"), list):
    complexity_data["domains"] = sorted(complexity_data["domains"])
if isinstance(complexity_data.get("reasons"), list):
    complexity_data["reasons"] = sorted(complexity_data["reasons"])
```

**验证方式**：R6 新增真实 subprocess 测试（`test_phase3_r6_review.py::TestCrossProcessHashStability`），启动 4 个不同 `PYTHONHASHSEED`（`0` / `1` / `42` / `12345`）的独立 Python 进程，验证 `plan_hash` 和 `registry_version` 完全一致。不再在同一进程内重复计算。

### 10.13 Canonical Intent Ordering（R6 P0-2）

R5 通过 `canonical_request_payload` 让 `request_hash` 对 `requested_tasks` 列表顺序无关，但 `resolve_agent_assignment` 仍按传入 Intent 顺序构造候选列表和笛卡尔积。其 Tie-breaker 分别排序 `agent_ids` 和 `versions`，丢失了"哪个 Agent 分配给哪个 Intent"的对应关系。语义相同的两个请求（仅 `requested_tasks` 顺序不同）会产生不同的 `agent_assignment` 和 `plan_hash`。

R6 修复策略 — 所有共享规划函数统一 Canonical Intent Order：

```python
canonical_intents = sorted(intents, key=lambda i: i.intent_id)
```

覆盖函数：

| 函数 | 使用 canonical_intents 的位置 |
|---|---|
| `resolve_agent_assignment` | 候选列表构造、笛卡尔积、Tie-breaker、assignment 返回 |
| `build_expected_planned_tasks` | PlannedTask 构造顺序、dependency task_id 映射 |
| Validator Canonical Reconstruction | `resolve_expected_intents` 返回的 intents 已按模板顺序，但 `resolve_agent_assignment` / `build_expected_planned_tasks` 内部统一排序 |

**Assignment Tie-breaker 保留映射**：

```python
assignment_key = tuple(
    (intent.intent_id, capability.agent_id, capability.version)
    for intent, capability in zip(canonical_intents, combo)
)
key = (total_auth, total_cost, total_timeout, assignment_key)
```

不再使用相互独立的 `sorted(agent_ids)` + `sorted(versions)`，避免丢失 Intent→Agent 映射。

不变量：

```
同一组 RequestedTask，仅列表顺序不同
→ 相同 canonical_intents
→ 相同 agent_assignment
→ 相同 plan_hash
```

### 10.14 Complete Plan Snapshot（R6 P0-3）

R5 的深快照只覆盖 `PlanDraft.request`，`complexity` 和 `tasks` 没有防御性复制。`plan.complexity is original_complexity` 和 `plan.tasks[0] is original_planned_task` 成立，调用方修改原始对象会破坏 PlanDraft。

R6 在 Contract 边界对全部三个字段强制深拷贝：

```python
@field_validator("complexity")
@classmethod
def _complexity_deep_snapshot(cls, v: ComplexityDecision) -> ComplexityDecision:
    return ComplexityDecision.model_validate(v.model_dump(mode="python"))

@field_validator("tasks")
@classmethod
def _tasks_deep_snapshot(cls, v: list[PlannedTask]) -> list[PlannedTask]:
    return [
        PlannedTask.model_validate(pt.model_dump(mode="python"))
        for pt in v
    ]
```

`PlannedTask` 内部的 `AgentTask` 和 `planning_metadata` 在重建 `PlannedTask` 时被 Pydantic 一并深拷贝。边界由 PlanDraft Contract 自身强制，手工构造也无法绕过。

不变量：

```
plan.request is original_request       → False
plan.complexity is original_complexity → False
plan.tasks[0] is original_planned_task → False
plan.tasks[0].task is original_task    → False
plan.tasks[0].planning_metadata is original_metadata → False
```

`build_execution_tasks()` 的现有防御性复制保留不变。

### 10.15 Immutable Authority Mapping（R6 P0-4）

R5 将 `TOOL_TO_AGENT_AUTHORITY` 从延迟初始化改为模块级 `dict`，但仍是可变字典，且通过 `multi_agent.__init__` 公开导出。`TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] = AgentAuthority.READ` 可全局降级权限边界。

R6 使用 `MappingProxyType` 不可变 Mapping：

```python
from types import MappingProxyType
from typing import Mapping

TOOL_TO_AGENT_AUTHORITY: Mapping[ToolAuthority, AgentAuthority] = MappingProxyType(
    {
        ToolAuthority.READ: AgentAuthority.READ,
        ToolAuthority.PROPOSE: AgentAuthority.PROPOSE,
        ToolAuthority.EXECUTE: AgentAuthority.EXECUTE,
    }
)
```

- `__setitem__` / `__delitem__` 抛出 `TypeError`
- 类型注解 `Mapping[ToolAuthority, AgentAuthority]` 让类型检查器明确只读契约
- `validate_intent_tool_authority` 每次读取该 Mapping，无法被运行时篡改

不变量：

```
TOOL_TO_AGENT_AUTHORITY[ToolAuthority.PROPOSE] = AgentAuthority.READ
→ TypeError: 'mappingproxy' object does not support item assignment

READ Intent + crm_writer.propose
→ PlanningInputError (始终拒绝，无法绕过)
```

### 10.16 Canonical Complexity Payload（R7 P0-1）

R6 之前，Validator 对 `ComplexityDecision` 只比较 `route`、`set(domains)`、`set(reasons)`、`requires_human_review`，明确跳过 `confidence`。`ComplexityDecision` 本身允许普通 `list[str]`，没有去重或非空校验。因此一个带重复 `domains` / `reasons` 或不同 `confidence` 的 Complexity 可以通过 Validator。

R7 引入共享纯函数 `canonical_complexity_payload`，作为 Complexity 相等性的唯一定义：

```python
def canonical_complexity_payload(decision: ComplexityDecision) -> dict[str, Any]:
    # domains → 去重、排序、拒绝空白
    # reasons → 去重、排序、拒绝空白
    # route → 原值
    # confidence → 原值（R7 起进入比较）
    # requires_human_review → 原值
```

`compute_plan_hash` 和 `PlanValidator._check_complexity_decision` 都使用该函数，确保只有一套比较规则。

**Validator 策略**：
- `canonical_complexity_payload(plan.complexity)` 抛出 `ValueError` → 返回 `complexity_decision_mismatch`
- `plan.compute_plan_hash()` 抛出 `ValueError` → 返回 `plan_hash_mismatch`
- Payload 不一致 → 返回 `complexity_decision_mismatch`，附带字段级差异诊断

不变量：

```
ComplexityDecision(domains=["support", "support"])
→ complexity_decision_mismatch

ComplexityDecision(reasons=["", "reason"])
→ complexity_decision_mismatch

ComplexityDecision(confidence=0.5) vs Gate(confidence=1.0)
→ complexity_decision_mismatch

ComplexityDecision(domains=["sales", "support"])
== ComplexityDecision(domains=["support", "sales"])
→ canonical payload 相同
```

### 10.17 Immutable Default Template（R7 P0-2）

`CustomerRecoveryTemplate` 之前只继承 `StrictContract`（`validate_assignment=True`），没有 `frozen=True`。全局 Singleton `DEFAULT_CUSTOMER_RECOVERY_TEMPLATE` 可被运行时修改：

```python
DEFAULT_CUSTOMER_RECOVERY_TEMPLATE.support_analysis_required = False  # 成功
```

R7 将模板设为真正不可变：

```python
class CustomerRecoveryTemplate(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
```

`__setattr__` 抛出 `ValidationError`，所有字段（`name` / `version` / `customer_context_required` / `support_analysis_required` / `sales_risk_analysis_required` / `knowledge_recommendation_required` / `recovery_metrics_required`）均不可变。

### 10.18 Template Version Binding（R7 P0-2）

模板生成的每个 `TaskIntent.metadata` 现在包含 `template_version`：

```python
metadata={
    "template": self.name,
    "template_version": self.version,
    "phase": "context",
}
```

`build_expected_planned_tasks` 将 `intent.metadata` 复制到 `PlannedTask.planning_metadata`，后者进入 Plan Hash 和 Canonical Plan 比较。模板版本变化会被 Validator 检测到：

```
Plan A (template version = ma-03.1.0)
Plan B (template version = ma-03.99.0)
→ plan_hash 不同
→ Canonical Reconstruction 检测到 planning_metadata 差异
```

### 10.19 Canonical Request Snapshot Comparison（R7 P0-3）

R5/R6 让 `request_hash` 对 `requested_tasks` 和 `dependencies` 列表顺序无关，但 `_check_request_snapshot` 仍使用 Pydantic 原始对象比较 `plan.request != request`。两个语义相同的请求（仅列表顺序不同）会被 Validator 拒绝：

```
Request A: requested_tasks = [i1, i2]
Request B: requested_tasks = [i2, i1]

request_hash(A) == request_hash(B)
plan_hash(A) == plan_hash(B)
但 PlanValidator.validate(request=B, plan=Planner(A)).valid == False
```

R7 将 Request Snapshot 比较改为 Canonical Payload 比较：

```python
plan_payload = canonical_request_payload(plan.request)
caller_payload = canonical_request_payload(request)

if plan_payload != caller_payload:
    request_snapshot_mismatch
```

不变量：

```
requested_tasks 排列不同但语义相同 → 接受
dependencies 排列不同但语义相同 → 接受
domain / task_type / objective / budget / actor 真正改变 → 拒绝
```

### 10.6 Canonical RetryPolicy Contract（R5 P0-1 + R6 P0-2 + R7 P0-4）

R4 之前，Phase 3 的 `build_expected_planned_tasks` 硬编码 `max_retries=0`，PlanValidator 也会重建并校验该值。因此通过真实 Phase 3 Planner + 真实 PlanValidator 进入 Phase 4 的计划，永远没有重试次数。R5 将 `RetryPolicy` 提升为正式 Canonical Planning Contract，贯穿以下所有阶段：

```
RequestedTask.retry_policy
    ↓ (planner)
TaskIntent.retry_policy
    ↓ (planner)
PlannedTask.retry_policy
    ↓ (build_expected_planned_tasks)
Canonical Plan Reconstruction
    ↓ (stable_hash)
Plan Hash
    ↓ (PlanValidator._compare_planned_task_fields)
PlanValidator 比较 retry_policy.max_retries 和 retryable_error_codes
```

**Contract 定义**（`planning.py`）：

```python
class RetryPolicy(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_retries: int = Field(default=0, ge=0, le=3)
    retryable_error_codes: frozenset[str] = Field(default_factory=frozenset)

    # R6 P1: 内容校验
    @field_validator("retryable_error_codes")
    @classmethod
    def _validate_retryable_error_codes(cls, v: frozenset[str]) -> frozenset[str]:
        # 1. strip 空白，拒绝空字符串
        # 2. 拒绝 NEVER_RETRYABLE_ERROR_CODES 中的 code
        ...
```

**Plan Hash 绑定**：`RetryPolicy` 是 `PlannedTask` 的字段，参与 Canonical Plan Reconstruction 和 `compute_plan_hash`。任何对 `max_retries` 或 `retryable_error_codes` 的篡改都会导致 `plan_hash` 不匹配。

**max_retries 上限**：`le=3`——超过 3 的值在 Pydantic 构造时被拒绝。Phase 4 的 `should_retry_result()` 读取 `PlannedTask.retry_policy.max_retries`（而非 `AgentTask.max_retries`），R3 的 `max_retries=0` 固定 Canonical 规则由 `RetryPolicy.max_retries` 表达。

**Error Code Allowlist**：`retryable_error_codes` 是 `frozenset[str]`，非空时只有 allowlist 中的 code 可以触发 retry。空集合表示"任何 retryable=True 的 error 都可重试"。R6 P1 在构造时校验：
- 拒绝空字符串和纯空白
- 拒绝 `NEVER_RETRYABLE_ERROR_CODES` 中的 code（运行时始终拒绝）

**`NEVER_RETRYABLE_ERROR_CODES`**（`planning.py`，planning 和 runtime 共享）：

```python
NEVER_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset({
    "invalid_receipt", "invalid_result", "usage_unavailable",
    "non_retryable_error", "run_deadline_exceeded",
    "tenant_mismatch", "agent_identity_mismatch",
    "cancelled", "kill_switch",
})
```

**Phase 4 执行语义**（详见 Phase 4 文档 §6）：

- R6 P0-2: `should_retry()` 纯函数读取 `RetryPolicy`，**不**读取 `task.max_retries`
- R7 P0-4: `should_retry_result()` 接受 `Sequence[AgentError]`，`error_code` 与 `retryable` 来自同一个 AgentError（不允许 `errors[0].error_code` + `any(e.retryable)` 拼接）
- `NEVER_RETRYABLE_ERROR_CODES` 同时用于 PlanValidator（构造时校验）和 runtime `should_retry_result()`（运行时始终拒绝）——确保 Plan Hash 中的 `retryable_error_codes` 和运行时拒绝列表使用同一份规范

**PlanValidator 字段比较**：

| 字段 | 校验规则 | Issue Code |
|---|---|---|
| `PlannedTask.retry_policy.max_retries` | `== Expected PlannedTask.retry_policy.max_retries` | `task_field_mismatch` |
| `PlannedTask.retry_policy.retryable_error_codes` | `== Expected PlannedTask.retry_policy.retryable_error_codes`（frozenset 相等） | `task_field_mismatch` |

**测试要求**：Retry 测试**必须**使用真实 `DeterministicPlanner` + 真实 `PlanValidator` + 真实 `SupervisorRuntime`，**不**得注入 `_AlwaysValidPlanValidator` 或手工篡改 `AgentTask.max_retries`。

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
| Customer Recovery 模板输入冲突（R3 P0-4） | `PlanningInputError` | — |
| 无 capable agent（含 EXECUTE-only） | `UnsupportedCapabilityError` | — |
| 预算超限 | `BudgetExceededPlanningError` / `PlanValidationError` | `*_budget_exceeded` / `deadline_exceeded` |
| Plan hash 不匹配 | `PlanIntegrityError` / `ValidationError` | `plan_hash_mismatch` / `request_hash_mismatch` |
| DAG 有环 | `PlanCycleError` | `cycle` / `intent_cycle` |
| Intent 绑定不一致 | `PlanValidationError` | `plan_intent_mismatch` / `unstable_task_id` / `idempotency_key_mismatch` / `planned_task_required_mismatch` / `duplicate_intent_id` |
| Multi-agent 不足两个 Agent | `PlanValidationError` | `multi_agent_too_few_agents` |
| **Agent 分配不一致**（R3 P0-2） | `PlanValidationError` | `agent_assignment_mismatch` |
| **Dependency 不一致**（R3 P0-1） | `PlanValidationError` | `dependency_mismatch` |
| **Required Evidence 不一致**（R3 P0-1） | `PlanValidationError` | `required_evidence_mismatch` |
| **Canonical Task 字段不一致**（R3 P0-3） | `PlanValidationError` | `task_field_mismatch` |
| **Plan-time 生命周期违规**（R3 P0-3） | `PlanValidationError` | `task_lifecycle_violation` |
| **Planner Version 不匹配**（R3 P0-5） | `PlanValidationError` | `planner_version_mismatch` |
| **分配搜索空间超限**（R3 P1） | `UnsupportedCapabilityError` | — |
| **预算预检失败**（R3 P1 + R4 P0-2） | `BudgetExceededPlanningError` | — |
| **无可行 diverse 分配**（R3 P1） | `UnsupportedCapabilityError` | — |
| **Intent 图校验失败**（R4 P0-1） | `PlanValidationError` / `PlanCycleError` / `PlanningInputError` | `duplicate_intent_id` / `missing_intent_dependency` / `intent_cycle` |
| **Intent / Tool Authority 不对齐**（R4 P0-3） | `PlanningInputError` | `tool_authority_mismatch` |
| **预算可行分配不存在**（R4 P0-2） | `BudgetExceededPlanningError` | — |
| **Write/Approval 请求缺 PROPOSE Intent**（R5 P0-1） | `PlanValidationError` / `PlanningInputError` | `write_request_missing_propose_intent` / `approval_request_missing_propose_intent` |

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
3. **单模板** — 只有 Customer Recovery 模板；其他 multi-agent 场景从 `RequestedTask` 显式映射 intents（R2 更新）。R4 起 `DeterministicPlanner` 不再接收 `customer_recovery_template` 构造参数 —— Phase 3 只支持默认模板，自定义模板注入留待未来阶段以共享模板上下文（id + version + content hash）的形式接入
4. **不执行** — Plan 生成后不执行任何 agent handler
5. **不改 Router** — 现有 Kafka/Router 工作流完全不受影响
6. **Authority 上限** — Phase 3 最高 `PROPOSE`，不选择 `EXECUTE` agent
7. **Multi-agent 分配** — 使用有界笛卡尔积搜索（`MAX_ASSIGNMENT_COMBINATIONS=1,000,000`）+ R4 预算可行性过滤；不实现 Branch-and-Bound 或动态规划优化器（R3 P1 + R4 P0-2 更新）

## 16. 共享纯函数（R3 + R4 + R5 + R6）

R3 将 Planner 的核心决策逻辑抽取为无副作用纯函数，位于 `multi_agent.planning` 模块。Planner 和 Validator **共同使用**这些函数，确保两边产生完全相同的 Canonical Plan。R4 新增 Intent Graph 校验和 Tool Authority 对齐两个共享纯函数。R5 新增 Write/Approval Requirement 校验和 Canonical Request Payload 两个共享纯函数。R6 将 `resolve_agent_assignment` 和 `build_expected_planned_tasks` 内部统一为 Canonical Intent Order，并保证跨进程 Hash 稳定性：

| 函数 | 签名 | 用途 |
|---|---|---|
| `resolve_expected_intents` | `(request, decision) -> list[TaskIntent]` | 从 Request + ComplexityDecision 派生 Expected Intents（R2 已有） |
| `resolve_candidate_agents` | `(intent, registry) -> list[AgentCapability]` | Tool-aware 候选筛选 + 稳定排序（R3 新增） |
| `resolve_agent_assignment` | `(request, decision, intents, registry) -> dict[str, AgentCapability]` | 全局确定性 Agent 分配 + 有界搜索 + **预算预检 + DAG deadline 过滤** + Fail-Closed（R3 新增，R4 P0-2 升级为 budget-aware，**R6 P0-2 内部使用 canonical_intents 排序 + assignment_key 保留 Intent→Agent 映射**） |
| `build_expected_planned_tasks` | `(request, intents, assignment) -> list[PlannedTask]` | 构建 Canonical PlannedTask（固定 timeout_ms / max_retries / priority / status / lifecycle 字段 + **planning_metadata**）（R3 新增，R4 P1-1 追加 metadata 复制，**R6 P0-2 内部使用 canonical_intents 排序**） |
| `_stable_task_id` | `(*, run_id, intent_id, task_type, agent_id) -> str` | 稳定 task_id 计算（R3 新增，Planner 和 Validator 共享） |
| **`validate_intent_graph`** | `(intents) -> list[str]` | **R4 P0-1 新增**：Intent 依赖图校验（duplicate id / missing dep / cycle），返回稳定 Issue Code 列表 |
| **`validate_intent_tool_authority`** | `(intent, registry) -> None` | **R4 P0-3 新增**：Intent preferred_authority 必须覆盖 required_tools 的最高 authority，违反抛 `PlanningInputError`（**R6 P0-4 读取不可变 `TOOL_TO_AGENT_AUTHORITY` Mapping**） |
| **`_estimate_assignment_deadline_ms`** | `(intents, assignment) -> int` | **R4 P0-2 内部辅助**：计算给定 assignment 的 DAG 关键路径 timeout 之和 |
| **`_longest_path_node_count`** | `(intents) -> int` | **R4 P0-2 内部辅助**：计算 Intent DAG 最长路径节点数，用于 max_iterations 预检 |
| **`validate_write_approval_requirements`** | `(request, intents) -> list[str]` | **R5 P0-1 新增**：requires_write / requires_approval 时至少一个 Intent 必须为 PROPOSE，返回稳定 Issue Code 列表 |
| **`canonical_request_payload`** | `(request) -> dict[str, Any]` | **R5 P0-3 新增**：构建顺序无关的 Request Hash payload（requested_tasks / dependencies / set 字段全部排序），由 `compute_request_hash` 调用（**R6 P0-1 使用 `mode="python"` 保留 frozenset**；**R7 P0-3 `_check_request_snapshot` 也使用该函数做 Canonical 比较**） |
| **`canonical_complexity_payload`** | `(decision) -> dict[str, Any]` | **R7 P0-1 新增**：构建 Complexity 的 Canonical Payload（domains/reasons 去重、排序、拒绝空白；confidence 参与比较），由 `compute_plan_hash` 和 `PlanValidator._check_complexity_decision` 共同使用 |

**设计约束**：

- Validator **不得**调用 `DeterministicPlanner.create_plan()`，否则会产生递归
- 所有纯函数无副作用，不修改 `request` / `registry` / `intent` / `capability`
- 所有纯函数在相同输入下产生相同输出（确定性）
- R4 起，Validator 在 Canonical Plan 重建前必须先调用 `validate_intent_graph` 和 `validate_intent_tool_authority`，确保非法 Request 不会让 `KeyError` / `IndexError` 在后续阶段逃逸
- **R6 起，所有 BaseModel 序列化必须使用 `mode="python"` 保留 `frozenset` 类型**，让 Canonicalizer 的 set/frozenset 分支排序；`mode="json"` 会将 frozenset 转为 list 并保留进程随机迭代顺序，导致跨进程 Hash 漂移
- **R6 起，`resolve_agent_assignment` 和 `build_expected_planned_tasks` 内部统一使用 `canonical_intents = sorted(intents, key=lambda i: i.intent_id)`**，确保 Planner 输出与 Intent 输入列表顺序无关

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
| `test_phase3_r3_review.py` | 35 | R3 反例：Agent Assignment 篡改、Dependency/Required Evidence 绑定、Canonical Task 字段、Customer Recovery 输入互斥、Planner Version、分配搜索上限、共享纯函数一致性 |
| `test_phase3_r4_review.py` | 15 | R4 反例：Intent Graph 校验、Budget-aware Assignment（deadline/工具/迭代预算预检 + 可行性过滤）、Intent/Tool Authority 对齐、PlannedTask.planning_metadata 篡改 |
| `test_phase3_r5_review.py` | 16 | R5 反例：Write/Approval Requirement 共享校验、PlanDraft 深快照 + build_execution_tasks 防御性复制、Semantic Request Hash（顺序不变量 + 语义变化必变）、TOOL_TO_AGENT_AUTHORITY 静态化 |
| `test_phase3_r6_review.py` | 16 | R6 反例：Cross-process Hash Stability（subprocess + PYTHONHASHSEED）、Canonical Intent Ordering（permuted requested_tasks 不变量）、Complete Plan Snapshot（complexity + tasks 深快照）、Immutable Authority Mapping（MappingProxyType 不可变） |
| `test_phase3_r7_review.py` | 15 | R7 反例：Canonical Complexity Payload（confidence / 重复 / 空白 拒绝）、Immutable Customer Recovery Template（frozen=True + template_version 绑定 Plan Hash）、Canonical Request Snapshot Comparison（permuted requested_tasks / dependencies 接受，语义变化拒绝） |
| **Phase 3 合计** | **227** | |
| Phase 2 回归 | 168 | 全部通过 |
| Phase 1 回归 | 76 | 全部通过 |
| **总计** | **471** | |

## 新增文件

```
agents/src/multi_agent/
├── planning_errors.py          # 10 个错误类型
├── planning.py                 # Contract + hash + effective sets + resolve_expected_intents
│                                # + resolve_candidate_agents + resolve_agent_assignment (budget-aware, R4)
│                                # + build_expected_planned_tasks + _stable_task_id (R3)
│                                # + validate_intent_graph + validate_intent_tool_authority (R4)
│                                # + _estimate_assignment_deadline_ms + _longest_path_node_count (R4)
│                                # + validate_write_approval_requirements + canonical_request_payload (R5)
│                                # + TOOL_TO_AGENT_AUTHORITY 静态化 (R5 P1-1)
│                                # + PlanDraft.request 深拷贝 + build_execution_tasks 防御性复制 (R5 P0-2)
│                                # + mode="python" 跨进程 Hash 稳定性 (R6 P0-1)
│                                # + canonical_intents + assignment_key 保留映射 (R6 P0-2)
│                                # + PlanDraft.complexity/tasks 深快照 (R6 P0-3)
│                                # + TOOL_TO_AGENT_AUTHORITY MappingProxyType 不可变 (R6 P0-4)
├── serialization.py            # _canonical_value + stable_hash 使用 mode="python" (R6 P0-1)
├── complexity_gate.py          # RuleBasedComplexityGate (含 Customer Recovery 互斥校验, R3)
├── planning_templates.py       # CustomerRecoveryTemplate
├── plan_validator.py           # PlanValidator (Canonical Plan Reconstruction + Intent Graph / Tool Authority / Write-Approval 校验)
└── planner.py                  # DeterministicPlanner (调用共享纯函数, R3 + R4 + R5 + R6; 无 customer_recovery_template 注入, R4)

agents/tests/unit/multi_agent/
├── test_complexity_gate.py
├── test_planner.py
├── test_plan_validator.py
├── test_planning_templates.py
├── test_phase3_r1_review.py    # R1 反例测试
├── test_phase3_r2_review.py    # R2 反例测试
├── test_phase3_r3_review.py    # R3 反例测试 (35 tests)
├── test_phase3_r4_review.py    # R4 反例测试 (15 tests)
├── test_phase3_r5_review.py    # R5 反例测试 (16 tests)
├── test_phase3_r6_review.py    # R6 反例测试 (16 tests, 含 subprocess 跨进程测试)
└── test_phase3_r7_review.py    # R7 反例测试 (15 tests)

docs/multi-agent/
└── phase-3-complexity-gate-planner.md  (本文件)
```

## 修改文件

- `agents/src/multi_agent/__init__.py` — 追加 Phase 3 导出 + `effective_domains` / `effective_task_types` / `resolve_expected_intents` / `MAX_ASSIGNMENT_COMBINATIONS` / `resolve_candidate_agents` / `resolve_agent_assignment` / `build_expected_planned_tasks`（R3）+ `validate_intent_graph` / `validate_intent_tool_authority` / `TOOL_TO_AGENT_AUTHORITY` / `CODE_INTENT_DUPLICATE_ID` / `CODE_INTENT_MISSING_DEPENDENCY` / `CODE_INTENT_CYCLE`（R4）
- `agents/src/multi_agent/planning.py` — 升级 `resolve_agent_assignment` 为 budget-aware（R4 P0-2）；新增 `validate_intent_graph` / `validate_intent_tool_authority` / `TOOL_TO_AGENT_AUTHORITY` / `_estimate_assignment_deadline_ms` / `_longest_path_node_count`（R4）；`PlannedTask.planning_metadata` 新字段（R4 P1-1）；`PLANNER_VERSION = ma-03.7.0`（R4→R5→R6→R7）；**R6 P0-1: `_canonical_tasks_payload` / `canonical_request_payload` / `compute_plan_hash` 改用 `mode="python"` + `ComplexityDecision.domains/reasons` 显式排序**；**R6 P0-2: `resolve_agent_assignment` / `build_expected_planned_tasks` 内部统一 `canonical_intents` 排序 + `assignment_key` 保留 Intent→Agent 映射**；**R6 P0-3: `PlanDraft` 新增 `complexity` / `tasks` 深快照 `field_validator`**；**R6 P0-4: `TOOL_TO_AGENT_AUTHORITY` 改为 `MappingProxyType` 不可变**；**R7 P0-1: 新增 `canonical_complexity_payload` 共享纯函数，`compute_plan_hash` 委托该函数**
- `agents/src/multi_agent/serialization.py` — **R6 P0-1: `_canonical_value` BaseModel 分支 + `stable_hash` 改用 `mode="python"` 保留 frozenset 类型，让 Canonicalizer 排序**
- `agents/src/multi_agent/registry.py` — **R6 P0-1: `_copy_capability` + `snapshot()` 改用 `mode="python"`，保证 `RegistrySnapshot.version` 跨进程稳定**
- `agents/src/multi_agent/planning_templates.py` — **R7 P0-2: `CustomerRecoveryTemplate` 新增 `frozen=True`；`build_intents` metadata 新增 `template_version` 字段**
- `agents/src/multi_agent/plan_validator.py` — `_check_canonical_plan` 新增 Step 1b（`validate_intent_graph`）和 Step 1c（`validate_intent_tool_authority`）；`_compare_planned_task_fields` 比较 `planning_metadata`（R4 P1-1）；删除 `CODE_AGENT_VERSION_MISMATCH` 及 per-task version 检查块（R4 P1-3）；新增 `CODE_TOOL_AUTHORITY_MISMATCH`；**R7 P0-1: `_check_complexity_decision` 使用 `canonical_complexity_payload` 完整 Payload 比较（含 confidence，去重，排序，拒绝空白）**；**R7 P0-1: `_check_plan_hash` 捕获 `ValueError` 返回 `plan_hash_mismatch`**；**R7 P0-3: `_check_request_snapshot` 使用 `canonical_request_payload` 比较替代原始 Pydantic 对象比较**
- `agents/src/multi_agent/planner.py` — 删除 `customer_recovery_template` 构造参数和 `self._recovery_template`（R4 P1-2）；`_validate_intents` 改为调用共享 `validate_intent_graph`（R4 P0-1）；新增 `_validate_tool_authority` 步骤调用 `validate_intent_tool_authority`（R4 P0-3）；`_HASH_CODES` 移除 `agent_version_mismatch`，新增 `missing_intent_dependency` / `tool_authority_mismatch`；`_CYCLE_CODES` 新增 `intent_cycle`
- `agents/tests/unit/multi_agent/test_phase3_r4_review.py` — 新增 R4 反例测试（15 tests）
- `agents/tests/unit/multi_agent/test_phase3_r5_review.py` — 新增 R5 反例测试（16 tests）
- `agents/tests/unit/multi_agent/test_phase3_r6_review.py` — **R6 新增反例测试（16 tests，含 subprocess 跨进程测试）**
- `agents/tests/unit/multi_agent/test_phase3_r7_review.py` — **R7 新增反例测试（15 tests）**
- `docs/multi-agent/phase-3-complexity-gate-planner.md` — 文档更新至 R7
