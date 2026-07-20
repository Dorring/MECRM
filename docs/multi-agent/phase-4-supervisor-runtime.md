# Phase 4: Supervisor Runtime + Dependency-Aware DAG Execution

**Status:** Complete  
**Branch:** `feat/ma-04-supervisor-runtime`  
**Baseline:** `main` (Phase 3, commit `d586e70`)

---

## 1. Supervisor Runtime 架构

Phase 4 的核心是 `SupervisorRuntime`——一个将已通过 Phase 3 验证的 `PlanDraft` 转换为可观察的、有界并发的执行流的编排器。

```
                  ┌─────────────────────────┐
                  │   SupervisorRuntime     │
                  │   .execute(plan, reg)   │
                  └────────────┬────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
  Pre-flight              RunStore.lease         DagScheduler
  Validation              (idempotency)          (wave loop)
        │                      │                      │
        │                      │              ┌───────┴───────┐
        │                      │              │               │
        │                      │              ▼               ▼
        │                      │      AgentInvoker      asyncio.Semaphore
        │                      │      .invoke()          (max_concurrency)
        │                      │              │
        │                      │              ▼
        │                      │      AgentInvocationReceipt
        │                      │      (result + usage)
        │                      │              │
        │                      │              ▼
        │                      │      validate_agent_result
        │                      │              │
        │                      │              ▼
        │                      │      _BudgetAccountant
        │                      │      (actual usage)
        │                      │              │
        │                      ▼              ▼
        │              RunStore.complete   merge_parallel_results
        │              (defensive copy)    (Phase 2 algorithm)
        │                      │
        └──────────────────────┴──────────────
                               │
                               ▼
                       SupervisorRunResult
                       (status + trace + usage)
```

**核心约束**:

- Supervisor **不**执行 `ActionProposal`——它只收集和验证
- Supervisor **不**写入 CRM、Kafka 或数据库
- Supervisor **不**修改 `PlanDraft`、`AgentRegistry`
- Supervisor **不**接入 Application Startup（Phase 5 处理）

---

## 2. Phase 3 Plan → Phase 4 Execution 边界

Phase 3 产出已通过验证的 `PlanDraft`：

```
PlanDraft
├── request            (frozen PlanningRequest snapshot)
├── complexity         (frozen ComplexityDecision)
├── tasks: list[PlannedTask]
│   ├── task: AgentTask          (timeout_ms, max_retries, ...)
│   ├── required: bool           (failure propagation)
│   └── planning_metadata
├── request_hash
├── plan_hash
├── registry_version
└── planner_version
```

Phase 4 通过 `plan.build_execution_tasks()` 拿到深拷贝的 `AgentTask` 列表，**不修改** PlanDraft 内部状态。

### Pre-flight 检查（任何 Task 开始前必须完成）

1. `plan.verify_integrity()` — `request_hash` 和 `plan_hash` 一致
2. `registry.snapshot().version == plan.registry_version` — 版本对齐
3. `PlanValidator.validate(plan.request, plan, registry).valid` — 重新验证
4. `RunStore.begin(run_id, plan_hash)` — 幂等检查
5. 所有 Handler 通过 Registry `resolve()` 可解析
6. `ExecutionUsage` 初始化为 0

任一检查失败 → 抛 `SupervisorError`，不调用任何 Handler。

---

## 3. Registry Handler Invocation

Phase 4 不让 Scheduler 直接依赖具体 Agent 实现。定义边界 Protocol：

```python
class AgentInvoker(Protocol):
    async def invoke(
        self,
        handler: AgentHandler,
        task: AgentTask,
        context: AgentExecutionContext,
    ) -> AgentInvocationReceipt: ...
```

`AgentInvocationReceipt` 封装：

- `result: AgentResult` — Handler 的返回值
- `tool_calls: int` — 实际工具调用次数
- `tokens_used: int | None` — 实际 token 使用量（可选）
- `cost_usd: Decimal | None` — 实际成本（可选）

**实现**:

- `RegistryAgentInvoker` — 生产实现，调用 `handler.run(task, context)`
- `DeterministicFakeInvoker` — 测试 stub，根据 `task.task_id` 返回预设结果

如果未来 Handler Protocol 改变签名，只需新增 Adapter，**不**批量修改 Specialist。

---

## 4. DAG Scheduler

`DagScheduler` 实现 Wave 循环算法：

```
wave 0:
  ready = [tasks with no dependencies or all deps completed]
  ready.sort(by task_id)         # 稳定调度
  async gather(ready, max_concurrency)

wave 1:
  ready = [tasks whose deps completed in wave 0]
  ...
```

### Ready 条件（必须全部成立）

- 所有 `dependencies` 已有终态（completed/failed/skipped/cancelled）
- 所有 `dependencies.status == completed`
- Task 自身 `status == pending`
- 未取消、未触发 Kill Switch
- 预算仍允许至少一次 Agent Call

### 稳定性保证

- Ready Queue 按 `task.task_id` 升序排序
- 同一波 Ready Task 可并发执行
- 相同 Plan + 相同 Fake Handler → Trace 顺序可重复
- **不**依赖输入 List 顺序

### 禁止行为

- 无界 `asyncio.gather()`
- Busy Loop
- 用 `sleep` 猜测任务完成
- 修改 PlanDraft 内部 Task
- 动态生成 Phase 3 Plan 中不存在的 Task

---

## 5. 并发模型

```python
semaphore = asyncio.Semaphore(config.max_concurrency)

async def _run_one(task):
    async with semaphore:
        return await invoker.invoke(handler, task, ctx)
```

**特性**:

- 同一波 Ready Task 受 `Semaphore` 约束
- `max_concurrency` 默认 4，范围 [1, 32]
- Dependency Task 完成后才能释放 Child
- 不依赖机器速度——`deterministic_mode=True` 时 Trace 顺序固定

---

## 6. Retry 规则

**总调用次数** = `1 + task.max_retries`

### 允许重试的情况

- Handler 抛 `RetryableAgentError`
- `AgentResult.error.retryable == True`
- Task Timeout 且仍有剩余 Deadline

### 禁止重试的情况

- Planning/Contract Validation Error
- Tenant Mismatch
- Agent ID Mismatch
- Task ID Mismatch
- `needs_input`
- `cancelled`
- Kill Switch 激活
- 非 retryable error

### Agent Call 预算统计

所有 Attempt（包括失败和 Timeout）都计入 `max_agent_calls` 预算。

---

## 7. Timeout

### Task Timeout

每次 Handler 调用使用 `asyncio.wait_for`:

```python
await asyncio.wait_for(
    invoker.invoke(handler, task, ctx),
    timeout=task.timeout_ms / 1000,
)
```

### Run Deadline

使用 `time.monotonic()` 跟踪 `plan.request.budget.deadline_ms`：

- 开始新 Attempt 前检查剩余时间
- **不**使用 `datetime.now()` 差值（跨平台不一致）
- 超过 deadline → 停止调度新 Task，状态置 `budget_exceeded`

---

## 8. Actual Budget Enforcement

Phase 4 **不**使用 Phase 3 的估算值。所有计数都基于实际 Invocation Receipt。

### max_tasks

执行前检查 `len(plan.tasks) <= budget.max_tasks`。

### max_agent_calls

每次 Attempt 开始前 +1。Retries 计入。

### max_tool_calls

每次 Invocation 完成后累加 `receipt.tool_calls`。超过预算 → 停止调度，状态置 `budget_exceeded`。

### max_iterations

每释放一波新的 Ready Task，Iteration +1。超过预算 → 停止调度。

### deadline_ms

基于 `time.monotonic()` 实际单调时钟。

### token_budget / cost_budget_usd

```
预算 == None             → 不校验
预算已设置但 receipt 无 usage → fail-closed: ExecutionUsageUnavailableError
预算已设置且有实际 usage    → 累计检查
```

---

## 9. Result Validation

每个 Handler 返回的 `AgentResult` 在进入 Merge 前必须通过 `validate_agent_result()`:

- `result.task_id == task.task_id`
- `result.agent_id == task.agent_id`
- `result.tenant_id == plan.tenant_id`
- `result.status` 属于允许值
- `Proposal.created_by_agent == task.agent_id`
- Proposal Tenant 一致
- Evidence Tenant 一致
- Evidence 引用完整
- Proposal Hash 完整

**无效 Result 处理**:

- 不进入 `merged_state`
- Task 标记 `failed`
- 记录 `invalid_agent_result` trace event
- **不**允许重试（除非错误明确为 retryable）

---

## 10. State Merge

每一波完成后或最终完成时调用 Phase 2 的 `merge_parallel_results`:

```python
from multi_agent.state import merge_parallel_results

merged_state = merge_parallel_results(
    valid_results,
    expected_tenant_id=plan.tenant_id,
)
```

**保证**:

- 冲突 Result 的子对象被排除
- 失效 Proposal 双路径排除（merged_proposals 和 results[*].action_proposals）
- Missing Evidence Proposal 排除
- Foreign Tenant 排除
- Merge 顺序无关

Phase 4 **不**实现第二套合并算法。

---

## 11. Failure Propagation

### Required Task failed

- Run 最终状态 = `failed`
- 所有依赖该 Task 的 Descendant → `skipped`
- 独立分支可继续执行（`continue_independent_branches=True`）

### Required Task needs_input

- Run 最终状态 = `needs_input`
- 依赖它的 Descendant → `skipped`
- 独立分支可继续执行

### Optional Task failed

- 独立 Required Tasks 可继续
- Run 最终状态 = `partial_success`

### Optional Task skipped

- **不**使 Run failed
- 若有其他 Required 完成 → `partial_success`

### Final Status Priority

```
cancelled
> budget_exceeded
> failed
> needs_input
> partial_success
> completed
```

`_compute_final_status` 按 `final_status_priority` 排序所有 candidate，取优先级最高者。

---

## 12. Cancellation / Kill Switch

### Protocol

```python
class ExecutionCancellation(Protocol):
    async def is_cancelled(self, run_id: str) -> bool: ...
    async def is_kill_switch_active(self, tenant_id: str) -> bool: ...
```

### 检查时点

- Run 开始前
- 每一 Scheduler Wave 前
- 每个 Task Invocation 前
- Retry 前

### 触发后行为

- 不再启动新 Task
- 等待或取消正在运行 Task
- pending Task → `cancelled`
- Run → `cancelled`

**Phase 4 不直接绑定生产 Kill Switch**——通过 `ExecutionCancellation` Protocol 隔离。`FakeExecutionCancellation` 用于测试。

---

## 13. Run Idempotency

### Protocol

```python
class RunStore(Protocol):
    async def begin(self, run_id: str, plan_hash: str) -> RunLease: ...
    async def complete(self, result: SupervisorRunResult) -> None: ...
```

### 行为

| 场景 | 行为 |
|---|---|
| 同 `run_id` + 同 `plan_hash` + 已完成 | 返回之前的**深拷贝**结果，不重复调用 Handler |
| 同 `run_id` + 不同 `plan_hash` | `RunPlanConflictError` |
| 同 `run_id` 正在执行 | `RunAlreadyInProgressError` |

`InMemoryRunStore` 是 Phase 4 的唯一实现——**不**持久化到数据库。`defensive_copy_result` 使用 `model_validate(model_dump(mode="python"))` 确保 `frozenset`/`Decimal`/`datetime` 类型完整保留。

---

## 14. LangGraph Adapter

```python
def build_supervisor_graph(runtime: SupervisorRuntime):
    ...
```

**5 个节点**:

1. `validate_plan` — 检查 `state.plan` 和 `state.registry` 已设置
2. `initialize_run` — 路由标记（实际 lease 在 Runtime 内部获取）
3. `execute_dag` — 调用 `runtime.execute()`，捕获异常到 `state.error`
4. `merge_results` — 路由标记（实际合并在 Runtime 内部）
5. `finalize_run` — 若 `state.error` 非空则重新抛出

**约束**:

- Graph 只包装 Runtime，**不**重复实现 Scheduler/Budget/Retry
- **不**修改现有 Chat Graph
- **不**自动注册到 Application Startup
- `FakeSupervisorRuntime` 可独立测试 Graph 路由

---

## 15. Customer Recovery 执行示例

使用 Phase 3 生成的五任务计划：

```
customer_context (root, required)
├── support_analysis (required)
├── sales_risk_analysis (required)
├── knowledge_recommendation (required)
└── recovery_metrics (required)
```

### 集成测试覆盖

- `customer_context` 首先执行
- 四个子任务只有在 root `completed` 后启动
- 四个子任务并发，受 `max_concurrency` 限制
- 所有结果被 merge
- Evidence 被保留
- ActionProposal 被收集但**没有执行**
- 最终状态 `completed`

### 额外场景

| 场景 | 期望状态 |
|---|---|
| support required failed | `failed` |
| sales needs_input | `needs_input` |
| knowledge optional failed | `partial_success` |
| root timeout | descendants `skipped`，状态 `failed` |
| max_agent_calls 不足 | `budget_exceeded` |
| Kill Switch 激活 | `cancelled` |

---

## 16. 为什么 ActionProposal 不在本阶段执行

Phase 4 的核心边界是**执行 Agent Task**，**不**执行业务副作用。

### 理由

1. **关注点分离** — Task 执行和 Proposal 执行是不同的失败模式。混在一起会让 retry/budget 语义模糊。
2. **审批流程** — `ActionProposal` 需要 Reviewer/Synthesizer/GovernedExecutor 介入，这些是 Phase 5 的内容。
3. **审计完整性** — Proposal 执行需要独立的审计日志和审批记录，不能与 Task 执行混在一起。
4. **可回滚** — Phase 4 的 Run 完成后，所有 Proposal 仍然是"提议"状态，可以被 Phase 5 接受、修改或拒绝。

### 实际行为

- Handler 可以返回 `ActionProposal`（作为 `AgentResult.action_proposals` 的一部分）
- Supervisor 收集所有 Proposal 到 `merged_state.merged_proposals`
- Proposal 经过 `validate_agent_result` 验证（hash、tenant、evidence 引用）
- Proposal **不**被传给 `GovernedExecutor` 或任何 CRM 写入路径

---

## 17. 已知限制

1. **无持久化** — `InMemoryRunStore` 在进程重启后丢失所有 Run 状态。Phase 5 添加 Postgres 实现。
2. **无 Reviewer/Synthesizer** — Phase 4 不对 Proposal 做语义审查，只做结构验证。
3. **无 Governed Executor** — Proposal 不被执行，需要 Phase 5 介入。
4. **无外部 LLM** — Phase 4 测试使用 `DeterministicFakeInvoker`，不连接真实 LLM/Ollama。
5. **无 Application Startup 集成** — Supervisor 不在 Application 启动时注册，需要显式调用。
6. **Token/Cost 依赖 Receipt** — 若 Handler 不报告 usage 且 budget 已设置，会 fail-closed。
7. **Retry 受 Phase 3 限制** — Phase 3 Planner 硬编码 `max_retries=0`。测试通过 `object.__setattr__` 篡改 task.max_retries 来验证 retry 路径。

---

## 18. Phase 5 接入点

Phase 5 将在以下位置接入：

1. **Reviewer** — 在 `merge_results` 节点后插入，对 `merged_state.merged_proposals` 做语义审查
2. **Synthesizer** — 合并多个 Proposal 为单一执行计划
3. **Governed Executor** — 执行经过审批的 Proposal，写入 CRM
4. **Application Startup** — 注册 `SupervisorRuntime` 和 `build_supervisor_graph` 到依赖注入
5. **Postgres RunStore** — 替换 `InMemoryRunStore`，支持跨进程幂等
6. **生产 Kill Switch Adapter** — 实现 `ExecutionCancellation` 绑定到现有 Kill Switch
7. **真实 LLM Invoker** — 替换 `DeterministicFakeInvoker`，接入 Provider Factory

Phase 4 的所有 Protocol（`AgentInvoker`、`RunStore`、`ExecutionCancellation`）都为 Phase 5 预留了替换点，无需修改 Runtime 核心逻辑。

---

## 附录 A: 文件清单

### 新增源文件

```
agents/src/multi_agent/
├── execution_errors.py     # 6 个错误类
├── invocation.py           # AgentInvoker Protocol + 2 实现
├── execution.py            # Contracts + helpers (SupervisorRunStatus, etc.)
├── scheduler.py            # DagScheduler + TaskOutcome
├── run_store.py            # RunStore Protocol + InMemoryRunStore
├── supervisor.py           # SupervisorRuntime
└── supervisor_graph.py     # LangGraph Adapter
```

### 新增测试

```
agents/tests/unit/multi_agent/
├── test_invocation.py                    # 13 tests — AgentInvoker boundary
├── test_scheduler.py                     # 11 tests — DAG wave + concurrency
├── test_execution_budget.py              # 27 tests — actual budget enforcement
├── test_supervisor.py                    # 23 tests — runtime + customer recovery
├── test_run_store.py                     # 18 tests — idempotency + defensive copy
└── test_supervisor_graph.py              # 8 tests  — LangGraph adapter routing
```

Customer Recovery 五任务执行场景（§15）作为集成测试嵌入在 `test_supervisor.py` 中，
覆盖：root 先执行、子任务并发、`max_concurrency` 限制、Required failed → `failed`、
Optional failed → `partial_success`、`needs_input`、timeout、`budget_exceeded`、Kill Switch。

### 修改文件

```
agents/src/multi_agent/__init__.py        # 追加 Phase 4 公共导出
```

---

## 附录 B: 关键 Contract 速查

### SupervisorRunStatus

```python
class SupervisorRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_SUCCESS = "partial_success"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
```

### SupervisorConfig

```python
class SupervisorConfig(StrictContract):
    max_concurrency: int = Field(default=4, ge=1, le=32)
    retry_backoff_ms: int = Field(default=0, ge=0)
    continue_independent_branches: bool = True
    deterministic_mode: bool = True
```

### Trace Event Types

```
run_started, plan_validated, task_ready, task_started, task_retrying,
task_completed, task_failed, task_needs_input, task_timed_out,
task_skipped, budget_exceeded, run_cancelled, results_merged, run_completed
```
