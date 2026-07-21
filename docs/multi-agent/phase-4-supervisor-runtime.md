# Phase 4: Supervisor Runtime + Dependency-Aware DAG Execution

**Status:** Complete  
**Branch:** `feat/ma-04-supervisor-runtime`  
**Baseline:** `main` (Phase 3, commit `d586e70`)

> **R5 Revision** — This document reflects the R5 audit fixes (commit `<TBD>`).
> R1 baseline: commit `e5ab368`. R2 baseline: commit `64fedd1` (5 P0 + 3 P1
> fixes, request-changes). R3 baseline: commit `5b9c647` (4 P0 + 3 P1 fixes,
> request-changes). R4 baseline: commit `bc5abd4` (4 P0 + 2 P1 fixes,
> request-changes). R5 addresses 5 P0 and 2 P1 issues from the R4 review,
> focused on **actual invocation vs dispatch permits, usage recording vs
> enforcement separation, and retry policy canonicalization**:
> RetryPolicy must be a Canonical Planning Contract that flows through the
> real Phase 3 → Phase 4 boundary (no more hardcoded `max_retries=0`);
> budget-denied Ready Tasks must produce `budget_exceeded` final status
> (DispatchDecision contract); Call Permits must be separated from Actual
> Agent Calls so `usage.agent_calls == invoker.invoke()` count; Usage
> Recording must always accumulate trusted usage regardless of whether the
> corresponding budget is configured; Provider Usage Trust must come from an
> authoritative `ProviderUsageVerifier` Adapter, not from Handler self-attested
> `provider_metadata`; Wave ordering must run pre-dispatch before iteration
> reservation; legacy `TrustedUsageInvoker` marker Protocol removed.

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

**R3 P0-1 + P1-1 调整后顺序**（R4 P0-1 强化决策矩阵）：

1. `plan.verify_integrity()` — `request_hash` 和 `plan_hash` 一致
2. **R3 P1-1 / R4 P0-1** `RunStore.lookup_run_identity(run_id, plan_hash)` — 只读 Probe，一次确定状态。**R4 P0-1 关键变化**：`plan_hash_matches` 必须**先于** `status` 检查，因此不同 Plan 的冲突**不会**被 Registry Version Mismatch 或 `RunAlreadyInProgressError` 掩盖。完整决策矩阵见 §13.2。
   - 同 `run_id` + **不同 `plan_hash`** → `RunPlanConflictError`（不论 `status`，在 Registry Pre-flight 之前）
   - 同 `run_id` + 同 `plan_hash` + completed → **cache hit**，直接返回深拷贝
   - 同 `run_id` + 同 `plan_hash` + running → `RunAlreadyInProgressError`
   - 未知 → 继续后续 Pre-flight
3. `registry.snapshot().version == plan.registry_version` — 版本对齐
4. `PlanValidator.validate(plan.request, plan, registry).valid` — 重新验证
5. **R2 P0-1** `_build_execution_bindings(plan, registry)` — 为每个 Task 一次性解析 `(capability, handler)`，构建不可变 `ExecutionBinding` + `bound_handlers` 映射；执行期间不再调用 `registry.resolve()`
6. **R3 P0-1** Async cancellation / Kill Switch pre-check — **位于 Registry / Validator / Binding 校验之后**。如果 Run 已取消或 Kill Switch 激活，直接 finalize 为 `cancelled`（不获取 lease、不预留 iteration）
7. `RunStore.begin(run_id, plan_hash)` — 获取 frozen RunLease（含 `lease_id`）
8. `ExecutionUsage` 初始化为 0

任一检查失败 → 抛 `SupervisorError`，不调用任何 Handler。

> **R3 P0-1 取消不得使无效 Plan 变成合法缓存**：取消检查移到 Registry/Validator/Binding 之后。一个 Hash 自洽但 Registry 过期 / Handler 不存在 / Validator 失败的 Plan，即使在调用时 Run 已被取消，也**不会**被缓存为 `cancelled` 结果——Pre-flight 先拒绝，再考虑取消路径。缓存命中的 Completed Result 仍可绕过 live Registry 漂移（这是预期的幂等行为）。

> **R4 P0-1 Plan Conflict 不可被掩盖**：旧版 Supervisor 在 Identity Probe 之后只判断 `identity.status`，不判断 `identity.plan_hash_matches`。如果同 `run_id` 已存在但 `plan_hash` 不同：completed 状态下会继续进入 Registry Pre-flight（最终以 `RegistryVersionMismatch` 失败），in-progress 状态下会以 `RunAlreadyInProgressError` 失败——两种情况都掩盖了真实的 `RunPlanConflictError`。R4 强制要求先检查 `plan_hash_matches`，mismatch 一律 `RunPlanConflictError`。

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
- **R3 P0-4** `usage_trust: UsageTrustLevel` — Usage 可信来源（见 §8）

**实现**:

- `RegistryAgentInvoker` — 生产实现，调用 `handler.run(task, context)`，默认 `verifies_tokens=False, verifies_cost=False`（除非显式配置 `usage_verifier` Adapter，详见 §8）
- `DeterministicFakeInvoker` — 测试 stub，根据 `task.task_id` 返回预设结果，默认 `usage_trust=UNVERIFIED`
- **R5 P0-5** `ProviderUsageVerifier` — 权威 Provider Usage 验证 Protocol，由 Supervisor 配置的可信 Adapter 实现，**不**能由 Handler 自证

如果未来 Handler Protocol 改变签名，只需新增 Adapter，**不**批量修改 Specialist。

### R3 P1-3: ExecutionBinding 是 _execute_task 的实际输入

R2 构造了 `ExecutionBinding` 和 `bound_handlers`，但执行过程只使用 `bound_handlers`——`ExecutionBinding.capability_snapshot` 没有传给 Invocation、Result Validation，也没进入 Trace。R2 的 `ExecutionBinding` 是未使用的审计外壳。

R3 选择 **方案 A（真正使用）**：

```python
class ExecutionBinding(StrictContract):
    model_config = {"extra": "forbid", "frozen": True}
    task_id: str
    agent_id: str
    capability_snapshot: AgentCapability
```

- `_build_run_task()` / `_execute_task()` 接受 `bindings: Mapping[task_id, ExecutionBinding]` 参数
- `_execute_task()` 从 `bindings[task.task_id]` 读取 `agent_id` / `capability_snapshot`，而非重新调用 `registry.resolve()`
- `TRACE_TASK_STARTED` trace event 携带 `binding_agent_id` / `binding_capability_agent_id` / `binding_capability_authority`，反映 Pre-flight 时的快照
- 即使 Registry 在 Run 期间漂移（Handler 替换、Capability 更新），Trace 中的 `binding_capability_*` 字段仍反映 Pre-flight 时的版本——审计可追溯

**方案 B（删除公共外壳）** 在 R3 不采用，因为 `ExecutionBinding` 现在已是实际执行的输入。

---

## 4. DAG Scheduler

`DagScheduler` 实现 Wave 循环算法：

```
wave 0:
  resolve skip propagation      # 依赖未完成的 pending → skipped
  should_stop?                  # sync budget/cancel check
  ready = [tasks with all deps completed]
  ready.sort(by task_id)        # 稳定调度
  before_wave(ready)?           # R2 P0-3: async cancel check
  on_wave_started(ready)        # reserve iteration + emit task_ready
  _run_wave_structured(ready)   # R2 P0-2: structured concurrency
  on_wave_completed(records)

wave 1:
  ...
```

### Ready 条件（必须全部成立）

- 所有 `dependencies` 已有终态（completed/failed/skipped/cancelled）
- 所有 `dependencies.status == completed`
- Task 自身 `status == pending`
- 未取消、未触发 Kill Switch
- 预算仍允许至少一次 Agent Call

### R2 P0-2: Structured Concurrency

`_run_wave_structured` 替换了 `asyncio.gather(*coros)`。旧实现使用 `gather(return_exceptions=True)` 会等待所有 Task 完成——但如果一个 Task 抛异常，兄弟姐妹 Task 仍在后台运行，可能导致 lease 释放后 Handler 继续执行（orphan side effects）。

新实现使用 `asyncio.wait(return_when=FIRST_EXCEPTION)`：

1. 为每个 Ready Task 创建 `asyncio.Task`，保留 `task → AgentTask` 映射
2. `asyncio.wait(tasks, return_when=FIRST_EXCEPTION)` — 任一 Task 抛异常立即返回
3. 如果有 pending Task（说明有 Task 抛异常），立即 cancel 所有 pending 并 `await gather(pending, return_exceptions=True)` 等待它们终止
4. 按 `ready` 顺序构建 outcomes：cancelled siblings → `TaskOutcome(status="cancelled")`；异常 Task → `TaskOutcome(status="failed")`；正常完成 → 原 result
5. 如果有异常，re-raise 第一个异常触发 Supervisor 的 abort 路径

**保证**：`_run_wave_structured` 返回或抛出时，所有 sibling Task 均已到达终态——不会有 Handler 在后台继续执行。

### R2 P0-3: Cancellation Wave Boundary

`before_wave` 是一个 `async` 钩子，在 `on_wave_started` **之前**调用。Scheduler 在调用 `before_wave` 时：

- **不**预留 iteration（`on_wave_started` 尚未调用）
- **不** emit `task_ready` trace event
- **不**创建 Task

如果 `before_wave` 返回 `True`（cancel/kill switch 激活）：
- 当前 wave 的所有 ready Task → `cancelled`
- 所有剩余 pending Task → `cancelled`
- 退出 wave 循环

**顺序保证**：
```
before_wave(ready)    ← FIRST: async cancellation check
on_wave_started(ready) ← SECOND: reserve iteration + emit task_ready
```

### R4 P1-1 / R5 P0-2 + P0-3 + P1-1: Deterministic Pre-Dispatch (Agent Call Budget)

R3 之前，同波 Ready Task 在创建协程后通过共享 `_BudgetAccountant` 竞争 Semaphore 预留 agent call slot。当 `max_agent_calls` 小于同波 Ready Task 数量时，哪个 Task 获得剩余 slot 依赖协程恢复顺序和事件循环实现——跨平台行为不一致。

R4 在 `on_wave_started` **之后**、协程创建**之前**插入同步 `pre_dispatch` 过滤器。**R5 P1-1 进一步调整 Wave 顺序**，将 `pre_dispatch` 移到 `on_wave_started` **之前**——避免为没有实际 Dispatch 的 Wave 消耗 Iteration 或 emit 误导性的 `task_ready`。

#### R5 P1-1: Wave Ordering

```
before_wave(ready)        ← async cancellation check
pre_dispatch(ready)       ← R5: moved BEFORE iteration reservation
  ├─ allowed 为空 → budget_exceeded，不创建 Wave
  └─ allowed 非空 → 继续
on_wave_started(allowed)  ← reserve iteration + emit task_ready ONLY for allowed
_run_wave_structured(allowed)
on_wave_completed(records)
```

**保证**：当所有 Ready Tasks 都因 Agent Call Budget 被拒时，**不**消耗 Iteration，**不** emit `task_ready`。

#### R5 P0-2: DispatchDecision Contract

R4 的 `pre_dispatch` 返回 `list[AgentTask]`——只能告诉 Scheduler "哪些任务可以执行"，无法区分"剩余任务被拒绝的原因"。R5 替换为显式的 `DispatchDecision`：

```python
class DispatchDecision(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed_task_ids: tuple[str, ...]    # 获得 Call Permit 的 task_id
    denied_task_ids: tuple[str, ...]    # 被拒绝的 task_id
    denial_reason: str | None           # 拒绝原因（如 "agent_call budget exhausted"）
    budget_exhausted: bool              # 是否因预算耗尽而拒绝
```

只要有 Ready Task 因 Agent Call Budget 无法启动：

```
accountant.exceeded = True
Run status = budget_exceeded
emit budget_exceeded trace
停止调度新任务
```

Budget Exceeded 的最终状态高于 Task Required/Optional 分类——**不**得将 Budget-denied Task 当作普通 Dependency Skip，也**不**得最终返回 `completed`。

#### R5 P0-3: Call Permit vs Actual Agent Call

R4 在 `pre_dispatch` 中直接调用 `reserve_agent_call()` 增加 Actual Usage。但 `_execute_task()` 随后会检查 Cancellation、Kill Switch、剩余 Deadline、Structured-concurrency sibling cancellation——任务可能获得 Slot 后根本没有调用 Handler，但 `usage.agent_calls` 已经增加。

R5 区分两个概念：

```python
class AgentCallPermit(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    permit_sequence: int  # Accountant 单调递增的序号
```

| 概念 | 触发点 | 是否计入 `usage.agent_calls` |
|---|---|---|
| **Dispatch Permit** | `pre_dispatch` 阶段，确定性发放 | ❌ 否 |
| **Actual Agent Call** | `await invoker.invoke(...)` 之前，`commit_agent_call(permit)` | ✅ 是 |

Permit 生命周期：

```
issue_permit(task_id)        ← pre_dispatch 阶段
    ↓
[commit_agent_call(permit)]  ← invocation 之前 → usage.agent_calls += 1
    OR
[release_permit(permit)]     ← cancellation / deadline / sibling cancel / 未使用 → 不计数
```

**保证**：

```
usage.agent_calls == Invoker 实际被调用的总次数
```

包括 Retry，但**不**包括：
- Pre-dispatch 后取消（permit 释放）
- Invocation 前 Deadline 耗尽（permit 释放）
- Structured-concurrency 在 Handler 开始前取消 sibling（permit 释放）
- 未使用的 Call Permit

#### R5 P0-2: Budget-denied Final Status

`can_start_iteration()` 和 `commit_agent_call()` **不**检查 `_exceeded` flag——因为 `_exceeded` 可能由当前 wave 的 `pre_dispatch` 设置（拒绝其他任务），但当前 wave 已发放 permit 的任务仍需执行。`should_stop` 在下一个循环迭代顶部检查 `_exceeded`，阻止未来 waves 启动。

### 稳定性保证

- Ready Queue 按 `task.task_id` 升序排序
- 同一波 Ready Task 可并发执行
- 相同 Plan + 相同 Fake Handler → Trace 顺序可重复
- **不**依赖输入 List 顺序

### 禁止行为

- 无界 `asyncio.gather()`
- `gather(return_exceptions=True)` 作为结构化并发替代（不会 cancel siblings）
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

### R5 P0-1: RetryPolicy Canonical Planning Contract

R4 之前，Phase 3 的 `build_expected_planned_tasks` 硬编码 `max_retries=0`，PlanValidator 也会重建并校验该值。因此通过真实 Phase 3 Planner + 真实 PlanValidator 进入 Phase 4 的计划，永远没有重试次数。R4 测试通过手工篡改 `AgentTask.max_retries` + 重新计算 Plan Hash + 注入 `_AlwaysValidPlanValidator` 绕过——这只证明 Retry 实现可以对篡改后的测试 Plan 工作，却没有证明它能通过正式的 Phase 3 → Phase 4 边界。

R5 将 RetryPolicy 提升为正式 Canonical Planning Contract：

```python
class RetryPolicy(StrictContract):
    max_retries: int = Field(default=0, ge=0, le=3)
    retryable_error_codes: frozenset[str] = Field(default_factory=frozenset)
```

`RetryPolicy` 贯穿以下所有阶段，任何篡改都会被 PlanValidator 检测到：

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

**测试要求**：Retry 测试**必须**使用真实 `DeterministicPlanner` + 真实 `PlanValidator` + 真实 `SupervisorRuntime`，**不**得注入 `_AlwaysValidPlanValidator` 或手工篡改 `AgentTask.max_retries`。

### 允许重试的情况

- Handler 抛 `RetryableAgentError`
- `AgentResult.error.retryable == True`
- Task Timeout 且仍有剩余 Deadline
- **R5 P0-1** `NonRetryableAgentError` / `InvalidInvocationReceiptError` 的 `error_code` 在 `retry_policy.retryable_error_codes` 中

### 禁止重试的情况

- Planning/Contract Validation Error
- Tenant Mismatch
- Agent ID Mismatch
- Task ID Mismatch
- `needs_input`
- `cancelled`
- Kill Switch 激活
- **R3 P0-2** `NonRetryableAgentError` — 显式 Agent Domain Error，标记 `failed` 并 break（不重试，不传播）
- 非 retryable error

### R3 P0-2: Exception Classification Boundary

`_execute_task()` 只捕获**明确的 Agent Domain Error**，未知异常**必须**传播到 Scheduler 的结构化并发边界：

| 异常类型 | 处理方式 | Siblings 影响 |
|---|---|---|
| `RetryableAgentError` | 转 `TaskExecutionRecord(status="failed", error_code="retryable_error")`，retry loop 继续 | 不取消 |
| `NonRetryableAgentError` | 转 `TaskExecutionRecord(status="failed", error_code="non_retryable_error")`，retry loop break | 不取消 |
| `InvalidAgentResultError` | 转 `TaskExecutionRecord(status="failed", error_code="invalid_result")`，不重试 | 不取消 |
| `InvalidInvocationReceiptError` | 转 `TaskExecutionRecord(status="failed", error_code="invalid_receipt")`，不重试 | 不取消 |
| `ExecutionUsageUnavailableError` | 转 `TaskExecutionRecord(status="failed", error_code="usage_unavailable")`，不重试 | 不取消 |
| `asyncio.TimeoutError` | 转 `TaskAttemptRecord(status="timed_out")`，根据剩余 Deadline 决定是否重试 | 不取消 |
| **`RuntimeError` / `TypeError` / `KeyError` / `AssertionError` / 其他未知异常** | **不捕获**——直接传播到 `_run_wave_structured()` | **取消同波所有 siblings 并 await** |

**关键约束**：
- **不存在** `except Exception` catch-all。R2 的 catch-all 会把 `RuntimeError` 等编程错误降级为普通 task failure，使 siblings 继续在损坏状态上运行，掩盖真实缺陷。
- 测试不得用 `BaseException` 绕过 Supervisor 的异常捕获——R3 测试使用 `RuntimeError` / `TypeError` 验证真实传播路径。

### Agent Call 预算统计

所有 Attempt（包括失败和 Timeout）都计入 `max_agent_calls` 预算。

---

## 7. Timeout

### Task Timeout

每次 Handler 调用使用 `asyncio.wait_for`:

```python
effective_timeout_s = min(task.timeout_ms, remaining_deadline_ms) / 1000.0
await asyncio.wait_for(
    invoker.invoke(handler, task, ctx),
    timeout=effective_timeout_s,
)
```

### Run Deadline

使用 `time.monotonic()` 跟踪 `plan.request.budget.deadline_ms`：

- 开始新 Attempt 前检查剩余时间
- **不**使用 `datetime.now()` 差值（跨平台不一致）
- 超过 deadline → 停止调度新 Task，状态置 `budget_exceeded`

### R2 P0-4: Deadline-aware Backoff

`_maybe_sleep` 在 retry backoff 时同时考虑 deadline 和 cancellation：

```python
sleep_ms = min(cfg.retry_backoff_ms, remaining_deadline_ms)
```

- 如果 `remaining_deadline_ms <= 0` → 标记 `deadline_exceeded`，返回 `"deadline_exceeded"`，不再 retry
- 如果 cancellation 激活 → 返回 `"cancelled"`，立即中断 backoff
- Backoff 期间以 10ms-100ms 间隔轮询 cancellation，确保 cancel 信号能及时唤醒 retry 循环

**R2 P0-4 Timer Jitter Fix**：当 `effective_timeout_s` 被 run deadline 而非 `task.timeout_ms` 限制时（`deadline_was_binding = remaining_deadline_ms <= task.timeout_ms`），任何 `TimeoutError` 都被归类为 `run_deadline_exceeded`——不再依赖 post-hoc `remaining_deadline_ms <= 0` 检查，因为 Windows 等平台的 timer 分辨率可能导致 `wait_for` 提前几毫秒触发，留下微小剩余时间从而错误分类为 `task_timeout`。

---

## 8. Actual Budget Enforcement

Phase 4 **不**使用 Phase 3 的估算值。所有计数都基于实际 Invocation Receipt。

### max_tasks

执行前检查 `len(plan.tasks) <= budget.max_tasks`。

### max_agent_calls

每次 Attempt 开始前 +1。Retries 计入。

### max_tool_calls

每次 Invocation 完成后累加 `receipt.tool_calls`。超过预算 → 停止调度，状态置 `budget_exceeded`。

### R4 P0-3: Observed Usage Accounting

R3 之前的处理顺序是 `validate_invocation_receipt() → record_receipt()`。如果 Handler 实际返回 10 条 `ToolCallRecord` 但 Receipt 错误声明 `tool_calls=0`：

- `validate_invocation_receipt()` 正确判定 Receipt 无效
- **不**调用 `record_receipt()` → Accountant 不增加任何 Tool Call
- 已经发生的 10 次实际工具调用从 Run Usage 中消失
- `max_tool_calls` 仍可能被后续任务继续消费

R4 将**观察到的实际 Usage** 和 **Receipt 声明值**拆开：

```python
observed_tool_calls = len(receipt.result.tool_calls)
accountant.record_observed_tool_calls(observed_tool_calls)  # BEFORE receipt validation

try:
    validate_invocation_receipt(receipt)
except InvalidInvocationReceiptError:
    # Receipt 不一致 → Task failed (invalid_receipt)
    # 但 observed_tool_calls 已经计入预算
    ...
else:
    accountant.record_receipt(receipt, invoker_capabilities=invoker_caps)
```

**保证**：无论 Receipt 是否通过一致性校验，已经发生的 Tool Call 都必须计入 `ExecutionUsage` 和 `max_tool_calls` enforcement。如果观察值已经超过预算，Accountant 标记 `_exceeded=True`，Scheduler 停止调度新任务。`TaskAttemptRecord` 也记录观察值，而不是错误的 Receipt 声明值。

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

### R5 P0-4: Usage Recording vs Enforcement 分离

R4 之前，`record_receipt()` 只在对应预算不为 `None` 时才累计 `tokens_used` / `cost_usd`。因此即使 Trusted Invoker 返回了有效的 `tokens_used=500, cost_usd=0.02`，只要 Request 没有设置 Token 或 Cost 上限，最终 `SupervisorRunResult.usage` 就可能仍报告为零。**预算是否配置只应决定"是否限制"，不应决定"是否记录"**。

R5 拆分两个独立概念：

| 概念 | 触发条件 | 行为 |
|---|---|---|
| **Usage Recording** | 可信且可用的 Usage | **始终累计**到 `ExecutionUsage`，无论 Budget 是否配置 |
| **Budget Enforcement** | 对应 Budget 不为 `None` | 额外检查是否超限 |

`ExecutionUsage` 新增 `tokens_usage_available` / `cost_usage_available` 字段，明确区分"零消耗"和"不可用"：

```python
class ExecutionUsage(StrictContract):
    agent_calls: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    cost_usd: Decimal = Decimal("0")
    iterations: int = 0
    # R5 P0-4: distinguish "zero consumption" from "data unavailable"
    tokens_usage_available: bool = False
    cost_usage_available: bool = False
```

**规则**：

```
可信 Usage → 始终累计，tokens_usage_available=True / cost_usage_available=True
Budget 为 None → 不限制（但仍记录）
Budget 已配置 → 记录后检查上限
```

**不**得用 `0` 同时表示"零消耗"和"无法获得数据"。

### R3 P0-4: Usage Provenance

R2 的 fail-closed 只处理 `receipt.cost_usd is None`，但自定义 Invoker 可以返回 `cost_usd = Decimal("0")` 绕过——系统会把它当成可信实际成本。Token 同理（`tokens_used = 0`）。

R3 为 Receipt 引入 **Usage Trust Level**：

```python
class UsageTrustLevel(StrEnum):
    VERIFIED_PROVIDER = "verified_provider"  # LLM Provider 原始返回
    TRUSTED_ADAPTER   = "trusted_adapter"    # 经审核的中间件
    UNVERIFIED        = "unverified"         # 自定义 Invoker 自报
```

`AgentInvocationReceipt` 新增 `usage_trust: UsageTrustLevel` 字段（默认 `UNVERIFIED`）。

### R4 P0-2 / R5 P0-5: Authoritative Provider Usage Verification

R3 的 `TrustedUsageInvoker` 标记 Protocol 是**可伪造**的——任意自定义 Invoker 可以直接在 Receipt 中设置 `usage_trust="trusted_adapter"` 而无需实际实现 `usage_is_verified=True`。R4 用 `UsageVerificationCapabilities` 替代 marker Protocol。

**R5 P0-5 进一步发现**：默认 `RegistryAgentInvoker` 仍可通过 Handler 自报 Metadata 获得 Token Trust。其可信判断仍依赖 Handler 返回的 `AgentResult.provider_metadata` 和 `AgentResult.token_usage`——只要 `provider_metadata` 非空，就可能将 Receipt 标记为 `verified_provider`。但这些字段本身仍由 Handler 构造，而不是由独立 Provider Adapter 或可信计费边界生成。

R5 引入**权威 Provider Usage Verifier** Protocol：

```python
class ProviderUsageVerifier(Protocol):
    def verify(
        self,
        *,
        source_id: str,
        provider_receipt: ProviderUsageReceipt,
    ) -> VerifiedUsage:
        ...

class VerifiedUsage(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tokens_used: int
    cost_usd: Decimal
    source_id: str
```

**默认 `RegistryAgentInvoker` 行为**（无 `usage_verifier` 参数时）：

```
verifies_tokens = False
verifies_cost = False
```

**只有**显式配置了权威 `ProviderUsageVerifier` Adapter 时，`RegistryAgentInvoker` 才声明能够验证 Provider Usage。Trust 来源**不**得由 `AgentResult.provider_metadata is not None` 推导。

#### R5 P0-5: Usage Capability Frozen Once Per Run

`Invoker` 的 `UsageVerificationCapabilities` 在 Run Pre-flight 时**一次性冻结**，并在整个 Run 中复用：

```python
# In SupervisorRuntime.execute() entry point:
invoker_caps = get_usage_capabilities(invoker)  # frozen ONCE

# Passed to _build_run_task() and _execute_task():
run_task = self._build_run_task(
    ...,
    invoker_caps=invoker_caps,  # frozen capability
)
```

**保证**：即使 Invoker 是可变对象并在 Run 期间修改其 `usage_capabilities` property，本 Run 的 Usage Trust 决策仍基于 Pre-flight 时的快照——**不**得在每个 Task 中动态重新读取可变 Invoker 属性。

#### R5 P1-2: Legacy Trust API Cleanup

R4 引入了 `UsageVerificationCapabilities`，但 R3 的 `TrustedUsageInvoker` marker Protocol 和 `usage_is_verified` 属性仍存在于公共导出。R5 **删除**这些未被 Runtime 使用的旧 API，公共导出和文档只保留 `UsageVerificationCapabilities` 及权威 `ProviderUsageVerifier` 模型——避免调用方误以为实现旧 Marker Protocol 即可获得 Trust。

#### 强制规则（在 `_BudgetAccountant.record_receipt()` 中执行）

| 配置 | Invoker Capability 要求 | 接受的 `usage_trust` |
|---|---|---|
| 未设置 Token/Cost Budget | 任何 | 任何（含 `UNVERIFIED`，但仍记录可用 Usage） |
| 设置 Token Budget | `verifies_tokens=True` | `VERIFIED_PROVIDER` 或 `TRUSTED_ADAPTER` |
| 设置 Cost Budget | `verifies_cost=True` | `TRUSTED_ADAPTER` |

Receipt 的 `usage_trust` **不得高于** Invoker 的 `usage_capabilities`：

```
unmarked invoker + receipt.usage_trust="trusted_adapter"  → ExecutionUsageUnavailableError
unmarked invoker + receipt.usage_trust="verified_provider" → ExecutionUsageUnavailableError
fake provider_metadata without verifies_tokens=True        → ExecutionUsageUnavailableError
receipt.usage_trust higher than invoker caps               → ExecutionUsageUnavailableError
```

`record_receipt()` 接受必需的 `invoker_capabilities` 参数：

```python
def record_receipt(
    self,
    receipt: AgentInvocationReceipt,
    *,
    invoker_capabilities: UsageVerificationCapabilities,
) -> None:
    ...
```

`RegistryAgentInvoker` 默认 `verifies_tokens=False, verifies_cost=False`（除非显式配置 `usage_verifier`）。`DeterministicFakeInvoker` 默认完全无 capability——除非显式传入 `usage_capabilities`。

---

## 9. Result Validation

每个 Handler 返回的 `AgentResult` 在进入 Merge 前必须通过 `validate_agent_result()`:

- `result.task_id == task.task_id`
- `result.agent_id == task.agent_id`
- `result.tenant_id == plan.tenant_id`
- `result.status` 属于允许值
- **R4 P0-4** `result.agent_version == binding.capability_snapshot.version`（当 `binding` 非空时）
- `Proposal.created_by_agent == task.agent_id`
- Proposal Tenant 一致
- Evidence Tenant 一致
- Evidence 引用完整
- Proposal Hash 完整

### R4 P0-4: Agent Version Binding

R3 将 `ExecutionBinding.capability_snapshot` 引入到 `_execute_task` 的实际执行路径，但 `validate_agent_result()` 只校验 `task_id` / `agent_id` / Tenant / Status / Evidence / Proposal，**不**校验 `result.agent_version`。一个 Handler 可以返回 `agent_id` 正确但 `agent_version` 是另一个版本的结果——该 Result 仍会进入 Merge，输出审计无法证明结果来自计划绑定的 Capability 版本。

R4 修改 `validate_agent_result()` 边界，新增 `binding: ExecutionBinding | None = None` 参数：

```python
def validate_agent_result(
    result: AgentResult,
    *,
    task: AgentTask,
    plan: PlanDraft,
    binding: ExecutionBinding | None = None,
) -> None:
    ...
    if binding is not None:
        bound_version = binding.capability_snapshot.version
        if result.agent_version != bound_version:
            raise InvalidAgentResultError(
                f"result.agent_version={result.agent_version!r} != "
                f"binding.capability_snapshot.version={bound_version!r}"
            )
```

`SupervisorRuntime._execute_task()` 调用时传入 `binding`：

```python
validate_agent_result(
    receipt.result,
    task=task,
    plan=plan,
    binding=binding,
)
```

**Trace 增强**：`TRACE_TASK_STARTED` 事件新增 `binding_capability_version` 字段，记录 Pre-flight 时绑定的 Capability 版本：

```python
trace.emit(
    TRACE_TASK_STARTED,
    task_id=task.task_id,
    agent_id=task.agent_id,
    data={
        "attempt": attempt_idx,
        "binding_agent_id": binding.agent_id,
        "binding_capability_agent_id": binding.capability_snapshot.agent_id,
        "binding_capability_authority": binding.capability_snapshot.authority.value,
        "binding_capability_version": binding.capability_snapshot.version,  # R4 P0-4
    },
    ...
)
```

**保证**：即使 Registry 在 Run 期间漂移（Handler 替换、Capability 更新），Trace 中的 `binding_capability_version` 仍反映 Pre-flight 时的版本——审计可追溯。Result 的 `agent_version` 必须与绑定版本一致才能进入 Merge。**不得**从 Live Registry 获取预期版本。

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
- 所有依赖该 Task 的 Descendant → `skipped`（dependency propagation）
- 独立分支可继续执行

### Required Task needs_input

- Run 最终状态 = `needs_input`
- 依赖它的 Descendant → `skipped`（dependency propagation）
- 独立分支可继续执行

### Required Task skipped (Handler-returned)

- **R2 P0-5** Run 最终状态 = `failed`（不再是 `partial_success`）
- Handler 主动返回 `result.status == "skipped"`，`skip_reason is None`
- 依赖它的 Descendant → `skipped`（dependency propagation，`skip_reason` 由 Scheduler 设置）

### Required Task skipped (dependency propagation)

- **R2 P0-5** 透明——不独立触发 `failed`
- `skip_reason` 由 Scheduler 设置（如 `"dependency 'X' status='failed'"`）
- Run 最终状态由父 Task 的实际状态决定（`failed` / `needs_input` / `cancelled`）

### Optional Task failed

- 独立 Required Tasks 可继续
- Run 最终状态 = `partial_success`

### Optional Task skipped

- **不**使 Run failed
- 若有其他 Required 完成 → `partial_success`

### R2 P0-5: 区分两种 skipped 来源

| 来源 | `skip_reason` | Required Task 影响 |
|------|---------------|-------------------|
| Handler-returned | `None` | Run = `FAILED` |
| Dependency propagation | Scheduler 设置 | 透明（由父 Task 决定） |

**Attempt 级别**：`_TaskAttemptStatus` 新增 `"skipped"` 成员。Handler 返回 `skipped` 时，Attempt 记录 `status="skipped"`（不再是 `cancelled`）。两种状态语义不同：`skipped` = 主动跳过，`cancelled` = 被动终止。

### Final Status Priority

**R3 P1-2 修正后顺序**：

```
cancelled
> budget_exceeded
> failed
> needs_input
> partial_success
> completed
```

`_finalize()` 按 R3 修正后的逻辑应用优先级：

1. **先**检查 Run-level Cancellation（`cancelled_during_run`）→ `CANCELLED`
2. **再**应用 `forced_status`（如 `BUDGET_EXCEEDED`）
3. **最后**取 `_compute_final_status` 的 computed status

R2 的旧顺序是 `forced_status > cancelled`，导致 max_tasks 超限触发 `forced_status=BUDGET_EXCEEDED` 时，即使 Cancellation 已激活，最终仍是 `BUDGET_EXCEEDED`——违反规范 `cancelled > budget_exceeded`。R3 修正为 Cancellation 永远优先。

---

## 12. Cancellation / Kill Switch

### Protocol

```python
class ExecutionCancellation(Protocol):
    async def is_cancelled(self, run_id: str) -> bool: ...
    async def is_kill_switch_active(self, tenant_id: str) -> bool: ...
```

### 检查时点

- **R2 P0-3** Run 开始前（async pre-run check，在 lease 获取之前）
- **R2 P0-3** 每一 Scheduler Wave 前（`before_wave` async 钩子，在 `on_wave_started` 之前）
- 每个 Task Invocation 前
- Retry 前（`_maybe_sleep` 内轮询）

### R2 P0-3: Pre-cancelled 路径

如果 Run 在 `execute()` 调用时已取消：
- **不**获取 lease
- **不**预留 iteration（`iterations == 0`）
- **不** emit `task_ready` / `task_started` trace events
- **不**调用任何 Handler
- 所有 Task → `cancelled`
- Run → `cancelled`

### 触发后行为

- 不再启动新 Task
- 等待或取消正在运行 Task（R2 P0-2 结构化并发保证 siblings 被 cancel + await）
- pending Task → `cancelled`
- Run → `cancelled`

**Phase 4 不直接绑定生产 Kill Switch**——通过 `ExecutionCancellation` Protocol 隔离。`FakeExecutionCancellation` 用于测试。

---

## 13. Run Idempotency

### Protocol

```python
class RunStore(Protocol):
    async def begin(self, run_id: str, plan_hash: str) -> RunLease: ...
    async def complete(self, lease: RunLease, result: SupervisorRunResult) -> None: ...
    async def abort(self, lease: RunLease, *, error_code: str) -> None: ...
    async def lookup_completed(self, run_id: str, plan_hash: str) -> SupervisorRunResult | None: ...
    # R3 P1-1: read-only identity probe
    async def lookup_run_identity(self, run_id: str) -> RunIdentity | None: ...
```

### 行为

| 场景 | 行为 |
|---|---|
| **R2 P0-1** 同 `run_id` + 同 `plan_hash` + 已完成 | `lookup_completed` 返回**深拷贝**结果，不检查 live registry 版本 |
| 同 `run_id` + 不同 `plan_hash` | `RunPlanConflictError` |
| 同 `run_id` 正在执行 | `RunAlreadyInProgressError` |

### R3 P1-1 / R4 P0-1: RunStore Identity Probe

`lookup_run_identity(run_id, plan_hash)` 是只读 Probe，返回 `RunIdentity`（frozen）：

```python
RunIdentityStatus = Literal["in_progress", "completed"]  # R4: 删除不可达的 "conflict"

class RunIdentity(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    requested_plan_hash: str  # R4 P0-1: 调用方传入的 plan_hash
    stored_plan_hash: str     # R4 P0-1: store 中已存在的 plan_hash
    status: RunIdentityStatus
    cached_result: SupervisorRunResult | None = None  # only when COMPLETED + matches

    @property
    def plan_hash_matches(self) -> bool:
        return self.requested_plan_hash == self.stored_plan_hash
```

Supervisor 在 Pre-flight 阶段调用一次，确定 cache/conflict/in-progress，**避免**在 Live Registry Pre-flight 之前抛 `RegistryVersionMismatch` 掩盖真实的 `RunPlanConflictError`。

#### R4 P0-1: Run Identity Probe Decision Matrix

R3 之前的 Supervisor 只判断 `identity.status`，不判断 `identity.plan_hash_matches`，导致：

- **completed + 不同 plan_hash** → 不抛 `RunPlanConflictError`，继续 Registry Pre-flight，最终以 `RegistryVersionMismatch` 失败（掩盖真实冲突）
- **in-progress + 不同 plan_hash** → 抛 `RunAlreadyInProgressError`（不是稳定的 `RunPlanConflictError`）

R4 强制要求 `plan_hash_matches` **先于** `status` 检查：

```python
identity = await store.lookup_run_identity(run_id, plan.plan_hash)

if identity is not None:
    if not identity.plan_hash_matches:
        raise RunPlanConflictError(
            f"run_id={run_id!r} is already bound to plan_hash="
            f"{identity.stored_plan_hash!r}, cannot accept "
            f"plan_hash={identity.requested_plan_hash!r}"
        )

    if identity.status == "completed":
        return identity.cached_result  # cache hit

    if identity.status == "in_progress":
        raise RunAlreadyInProgressError(...)
```

**决策矩阵**：

| `identity` | `plan_hash_matches` | `status` | 行为 |
|---|---|---|---|
| `None` | — | — | 未知 run，继续 Pre-flight |
| 非空 | `False` | 任意 | **`RunPlanConflictError`**（不论 completed/in-progress） |
| 非空 | `True` | `completed` | cache hit，返回 `cached_result` 深拷贝 |
| 非空 | `True` | `in_progress` | **`RunAlreadyInProgressError`** |

**额外保证**：

- `RunIdentityStatus` 类型收窄为 `Literal["in_progress", "completed"]`——R3 声明的 `"conflict"` 状态从未被 `InMemoryRunStore` 返回，是不可达值，R4 删除。
- `requested_plan_hash` 和 `stored_plan_hash` 作为独立字段保留，便于冲突诊断（日志可以同时输出两个 hash 值）。
- `cached_result` 仅在 `status="completed" + plan_hash_matches=True` 时填充，否则为 `None`。

### R3 P0-3: Frozen RunLease + Three-part Identity

R2 的 `RunLease` 是普通可变类，`complete()` 只验证 `lease.lease_id == entry.lease_id`，不验证 `entry.plan_hash == lease.plan_hash`——可以通过 `object.__setattr__` 篡改 `lease.plan_hash` 把原本绑定 `hash-a` 的活动 Lease 完成为 `hash-b`。

R3 将 `RunLease` 改为 `StrictContract` + `ConfigDict(frozen=True, extra="forbid")`：

```python
class RunLease(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    plan_hash: str
    lease_id: str  # secrets.token_hex(16), generated at begin()
    cached_result: SupervisorRunResult | None = None
```

`complete()` 和 `abort()` 都验证**三元身份**：

```
entry.run_id    == lease.run_id
entry.plan_hash == lease.plan_hash
entry.lease_id  == lease.lease_id
```

任一字段不一致 → `SupervisorError`。`plan_hash` 不再是"仅供参考"。

### R2 P1-1: Lease Identity（保留）

每个 `RunLease` 携带不可预测的 `lease_id`（`secrets.token_hex(16)`）。`complete` 和 `abort` 都验证 `lease_id`：

- **Stale `complete`**（cancelled coroutine 在 abort 后恢复）→ `SupervisorError("lease_id mismatch")`
- **Stale `abort`**（旧 lease 尝试删除新 lease）→ `SupervisorError("lease_id mismatch")`
- **`complete` identity check**：`lease.run_id == result.run_id` 且 `lease.plan_hash == result.plan_hash`，否则 `SupervisorError("identity does not match")`

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
7. **R5 P0-5 ProviderUsageVerifier 默认未配置** — 默认 `RegistryAgentInvoker` 不声明能够验证 Provider Usage（`verifies_tokens=False, verifies_cost=False`）。生产部署需要显式配置权威 Provider Usage Adapter 才能让 Token/Cost Budget enforcement 接受 `verified_provider` Receipts。

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
├── execution_errors.py     # 8 个错误类（R3 新增 NonRetryableAgentError）
├── invocation.py           # AgentInvoker Protocol + 2 实现 + UsageTrustLevel/TrustedUsageInvoker (R3)
├── execution.py            # Contracts + helpers (SupervisorRunStatus, ExecutionBinding, etc.)
├── scheduler.py            # DagScheduler + TaskOutcome
├── run_store.py            # RunStore Protocol + InMemoryRunStore + RunIdentity (R3)
├── supervisor.py           # SupervisorRuntime
└── supervisor_graph.py     # LangGraph Adapter
```

### 新增测试

```
agents/tests/unit/multi_agent/
├── test_invocation.py                    # AgentInvoker boundary
├── test_scheduler.py                     # DAG wave + concurrency
├── test_execution_budget.py              # actual budget enforcement
├── test_supervisor.py                    # runtime + customer recovery
├── test_run_store.py                     # idempotency + defensive copy + R3 frozen lease + identity probe
├── test_supervisor_graph.py              # LangGraph adapter routing
├── test_supervisor_r1.py                 # R1 regression (P0-1..P0-4)
├── test_supervisor_r2.py                 # R2 regression (P0-1..P0-5, P1-1..P1-2) — BaseException→RuntimeError (R3)
├── test_supervisor_r3.py                 # R3 regression (P0-1..P0-4, P1-2..P1-3) — 22 tests
├── test_supervisor_r4.py                 # R4 regression (P0-1..P0-4, P1-1) — 23 tests
└── test_supervisor_r5.py                 # R5 regression (P0-1..P0-5, P1-1..P1-2) — 22 tests
```

Customer Recovery 五任务执行场景（§15）作为集成测试嵌入在 `test_supervisor.py` 中，
覆盖：root 先执行、子任务并发、`max_concurrency` 限制、Required failed → `failed`、
Optional failed → `partial_success`、`needs_input`、timeout、`budget_exceeded`、Kill Switch。

R2 反例测试（`test_supervisor_r2.py`）覆盖：
- **P0-1** 缓存优先于 registry 版本检查、preflight bound handlers 不可被 registry mutation 替换
- **P0-2** wave 异常 cancel+await siblings、无 orphan tasks、lease 在 siblings 终止后才 abort
  （R3 修正：所有结构化并发测试改用 `RuntimeError` 替代 `BaseException`，验证真实传播路径）
- **P0-3** pre-cancelled 消费 0 iteration、emit 0 task_ready、between-waves 取消不预留下一轮
- **P0-4** backoff 被 deadline 封顶、被 cancellation 中断、timer jitter 不再误分类
- **P0-5** Required Handler-skipped → FAILED、dependency-propagation skipped 透明、attempt 记录真实 skipped
- **P1-1** stale lease 无法 abort/complete 新 lease、plan_hash identity 校验
- **P1-2** cost_budget_usd 配置但 invoker 报告 `cost_usd=None` → fail-closed

R3 反例测试（`test_supervisor_r3.py` + `test_run_store.py` 追加）覆盖：
- **P0-1** 预取消的 Run 仍需通过 Registry/Validator/Binding Pre-flight；Cancelled Result 仅在 Pre-flight 通过后缓存
- **P0-2** `RuntimeError` / `TypeError` 传播到 Scheduler 并取消 siblings；`NonRetryableAgentError` 被捕获为 task failure 不取消 siblings；未知异常不被降级
- **P0-3** `RunLease` 是 frozen StrictContract；`complete()` / `abort()` 验证三元身份（run_id + plan_hash + lease_id）
- **P0-4** unverified receipt 的 0/None/正数 usage 在 budget 配置时 fail-closed；verified_provider / trusted_adapter 接受
- **P1-1** `lookup_run_identity` 只读 Probe 在 Registry Pre-flight 之前确定 cache/conflict/in-progress
- **P1-2** Run-level Cancellation 优先于 `forced_status=BUDGET_EXCEEDED`
- **P1-3** `ExecutionBinding` 传入 `_execute_task`，`TRACE_TASK_STARTED` emit `binding_capability_*`，capability snapshot 不受 registry 漂移影响

R4 反例测试（`test_supervisor_r4.py`，23 tests）覆盖：
- **P0-1** Run Identity Probe Decision Matrix — completed+不同 plan → `RunPlanConflictError` 在 Registry check 之前；in-progress+不同 plan → `RunPlanConflictError`（非 `RunAlreadyInProgressError`）；registry drift 不掩盖 conflict；`RunIdentityStatus` 不含 `"conflict"`
- **P0-2** Invoker-bound Usage Trust — unmarked invoker 无法自报 `trusted_adapter` / `verified_provider`；fake `provider_metadata` 不创建 trust；Receipt 不能提升 trust 高于 Invoker capability；token/cost trust capability 在 Invoker 上检查
- **P0-3** Observed Tool Call Accounting — invalid receipt 仍计入实际 tool calls；under-reporting 不能保留预算；over-reporting 使用观察值；invalid receipt 触发预算超限停止新任务；`TaskAttemptRecord` 使用观察值
- **P0-4** Agent Version Binding — result `agent_version` mismatch 被拒绝；`binding_capability_version` 进入 trace；registry drift 不改变预期 result version；result version 校验基于 binding 而非 live registry
- **P1-1** Deterministic Call Slot Allocation — agent call budget 按 `task_id` 顺序选择；跨平台确定性；无 call slot 的 ready task 不启动 Handler

R5 反例测试（`test_supervisor_r5.py`，22 tests）覆盖：
- **P0-1** RetryPolicy Canonical — default RetryPolicy 流过真实 Planner；custom RetryPolicy 流过真实 Planner；篡改 `max_retries` 被真实 PlanValidator 检测；篡改 `retryable_error_codes` 被真实 PlanValidator 检测
- **P0-2** Budget-denied Ready Task — denied task 标记 skipped with budget reason；budget exhausted 终态为 `budget_exceeded`；denied task 零 attempts；`DispatchDecision` contract 携带 denial fields
- **P0-3** Call Permit vs Actual Call — `agent_calls` 等于 invoker.invoke() 次数；denied task 不调用不计数；deadline 释放 permit 不计数；committed permit 即使 invoker 抛错也计数；`AgentCallPermit` contract frozen
- **P0-4** Usage Recording vs Enforcement — verified tokens 在无 token budget 时仍记录；verified cost 在无 cost budget 时仍记录；unverified receipt 不被记录为 available；token budget enforcement 只在配置时触发
- **P0-5** Provider Usage Verification — 默认 `RegistryAgentInvoker` unverified；配置 verifier 后拥有 verified caps；invoker caps 在 pre-flight 冻结；`ProviderUsageVerifier` Protocol contract；`VerifiedUsage` contract frozen

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
    # R1 P1: continue_independent_branches and deterministic_mode
    # removed (never read by Scheduler/Supervisor; extra='forbid'
    # rejects them if passed).
```

### Trace Event Types

```
run_started, plan_validated, task_ready, task_started, task_retrying,
task_completed, task_failed, task_needs_input, task_timed_out,
task_skipped, budget_exceeded, run_cancelled, results_merged, run_completed
```

**R3 P1-3 / R4 P0-4**：`task_started` event 的 `data` 字段新增：

```
binding_agent_id             — ExecutionBinding.agent_id
binding_capability_agent_id  — ExecutionBinding.capability_snapshot.agent_id
binding_capability_authority — ExecutionBinding.capability_snapshot.authority
binding_capability_version   — R4 P0-4: ExecutionBinding.capability_snapshot.version
```

### Error Hierarchy（R3 更新）

```
MultiAgentError
└── SupervisorError                      # Phase 4 base
    ├── RetryableAgentError              # 可重试的 Agent Domain Error
    ├── NonRetryableAgentError           # R3 P0-2: 不可重试的 Agent Domain Error（不传播）
    ├── InvalidAgentResultError          # Result boundary 校验失败
    ├── InvalidInvocationReceiptError    # Receipt 一致性校验失败
    ├── ExecutionUsageUnavailableError   # R3 P0-4: Usage provenance/缺失 fail-closed
    ├── RunPlanConflictError             # 同 run_id 不同 plan_hash
    └── RunAlreadyInProgressError        # 同 run_id 正在执行
```

### R3 / R4 新增 Contract 速查

```python
# R3 P0-3: Frozen RunLease
class RunLease(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    plan_hash: str
    lease_id: str  # secrets.token_hex(16)
    cached_result: SupervisorRunResult | None = None

# R3 P1-1 / R4 P0-1: RunStore Identity Probe
RunIdentityStatus = Literal["in_progress", "completed"]  # R4: 删除不可达的 "conflict"

class RunIdentity(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    requested_plan_hash: str  # R4 P0-1
    stored_plan_hash: str     # R4 P0-1
    status: RunIdentityStatus
    cached_result: SupervisorRunResult | None = None

    @property
    def plan_hash_matches(self) -> bool: ...

# R3 P0-4: Usage Trust Level
class UsageTrustLevel(StrEnum):
    VERIFIED_PROVIDER = "verified_provider"
    TRUSTED_ADAPTER   = "trusted_adapter"
    UNVERIFIED        = "unverified"

# R4 P0-2: Invoker-bound Usage Trust（替代 R3 的 TrustedUsageInvoker marker Protocol）
class UsageVerificationCapabilities(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str

def get_usage_capabilities(invoker: object) -> UsageVerificationCapabilities:
    """getattr-based extraction; defaults to fully-unverified."""

# R3 P1-3 / R4 P0-4: ExecutionBinding（R3 使其成为 _execute_task 实际输入，
# R4 使其成为 validate_agent_result 的版本校验源）
class ExecutionBinding(StrictContract):
    model_config = {"extra": "forbid", "frozen": True}
    task_id: str
    agent_id: str
    capability_snapshot: AgentCapability

# R4 P1-1: Deterministic Pre-Dispatch (R5 P0-2: replaced by DispatchDecision)
PreDispatch = Callable[[list[AgentTask]], DispatchDecision]

# R5 P0-1: RetryPolicy Canonical Planning Contract
class RetryPolicy(StrictContract):
    max_retries: int = Field(default=0, ge=0, le=3)
    retryable_error_codes: frozenset[str] = Field(default_factory=frozenset)

# R5 P0-2: DispatchDecision
class DispatchDecision(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed_task_ids: tuple[str, ...]
    denied_task_ids: tuple[str, ...]
    denial_reason: str | None
    budget_exhausted: bool

# R5 P0-3: AgentCallPermit
class AgentCallPermit(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    permit_sequence: int

# R5 P0-4: ExecutionUsage with availability flags
class ExecutionUsage(StrictContract):
    agent_calls: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    cost_usd: Decimal = Decimal("0")
    iterations: int = 0
    tokens_usage_available: bool = False  # distinguish "zero" from "unavailable"
    cost_usage_available: bool = False

# R5 P0-5: Authoritative Provider Usage Verifier
class ProviderUsageVerifier(Protocol):
    def verify(
        self,
        *,
        source_id: str,
        provider_receipt: ProviderUsageReceipt,
    ) -> VerifiedUsage: ...

class VerifiedUsage(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tokens_used: int
    cost_usd: Decimal
    source_id: str
```
