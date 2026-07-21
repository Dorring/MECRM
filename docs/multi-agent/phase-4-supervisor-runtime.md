# Phase 4: Supervisor Runtime + Dependency-Aware DAG Execution

**Status:** Complete  
**Branch:** `feat/ma-04-supervisor-runtime`  
**Baseline:** `main` (Phase 3, commit `d586e70`)

> **R7 Revision** — This document reflects the R7 audit fixes (commit `<TBD>`).
> R1 baseline: commit `e5ab368`. R2 baseline: commit `64fedd1` (5 P0 + 3 P1
> fixes, request-changes). R3 baseline: commit `5b9c647` (4 P0 + 3 P1 fixes,
> request-changes). R4 baseline: commit `bc5abd4` (4 P0 + 2 P1 fixes,
> request-changes). R5 baseline: commit `f2288f8` (5 P0 + 2 P1 fixes,
> request-changes). R6 baseline: commit `d5fd130` (5 P0 + 2 P1 fixes,
> request-changes). R7 addresses 5 P0 and 2 P1 issues from the R6 review,
> focused on **no-provider-call attestation by a trusted boundary (not Handler
> omission), per-dimension independent coverage, multi-error retry pairing,
> async verifier bounded by deadline, and provenance source binding**:
> `AttemptUsageDisposition` replaces the R6 heuristic that inferred
> `no_provider_call` from `provider_metadata is None` — Handler can no longer
> self-attest by omitting a field; `NO_PROVIDER_CALL` requires
> `can_attest_no_provider_call=True` from a trusted Invoker;
> `record_usage_unavailable()` is invoked on the invalid-receipt path so Token
> and Cost are both marked `UNAVAILABLE`; Token and Cost have **independent**
> coverage denominators (`token_usage_applicable_attempts` /
> `cost_usage_applicable_attempts`) and per-attempt `AttemptUsageRecord` — a
> cost-only adapter verifying Attempt B's cost can no longer "offset" Attempt
> A's missing cost; `should_retry_result()` takes a `Sequence[AgentError]`
> and pairs `error_code` + `retryable` from the SAME error (no more
> `errors[0].error_code` + `any(e.retryable)`); `ProviderUsageVerifier.verify()`
> is `async def` and bounded by `asyncio.wait_for` (task timeout + run deadline
> + cancellation); `UsageVerificationCapabilities.bound_source_ids` rejects
> receipts whose `usage_provenance.source_id` is not in the invoker's bound
> set. P1: legacy `usage_trust` / `UsageTrustLevel` marked DEPRECATED; Phase 3
> documentation synchronized with the Canonical `RetryPolicy` contract.

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

**R3 P0-1 + P1-1 调整后顺序**（R4 P0-1 强化决策矩阵，R6 P0-5 调整 Invoker Capability 冻结位置）：

1. `plan.verify_integrity()` — `request_hash` 和 `plan_hash` 一致
2. **R3 P1-1 / R4 P0-1** `RunStore.lookup_run_identity(run_id, plan_hash)` — 只读 Probe，一次确定状态。**R4 P0-1 关键变化**：`plan_hash_matches` 必须**先于** `status` 检查，因此不同 Plan 的冲突**不会**被 Registry Version Mismatch 或 `RunAlreadyInProgressError` 掩盖。完整决策矩阵见 §13.2。**R6 P0-5 关键变化**：cache hit 路径在此步骤终止，**不**读取任何 Live Invoker 状态（详见 §8 R6 P0-5）。
   - 同 `run_id` + **不同 `plan_hash`** → `RunPlanConflictError`（不论 `status`，在 Registry Pre-flight 之前）
   - 同 `run_id` + 同 `plan_hash` + completed → **cache hit**，直接返回深拷贝（**R6 P0-5**: 不读取 `invoker.usage_capabilities`）
   - 同 `run_id` + 同 `plan_hash` + running → `RunAlreadyInProgressError`
   - 未知 → 继续后续 Pre-flight
3. `registry.snapshot().version == plan.registry_version` — 版本对齐
4. `PlanValidator.validate(plan.request, plan, registry).valid` — 重新验证
5. **R2 P0-1** `_build_execution_bindings(plan, registry)` — 为每个 Task 一次性解析 `(capability, handler)`，构建不可变 `ExecutionBinding` + `bound_handlers` 映射；执行期间不再调用 `registry.resolve()`
6. **R3 P0-1** Async cancellation / Kill Switch pre-check — **位于 Registry / Validator / Binding 校验之后**。如果 Run 已取消或 Kill Switch 激活，直接 finalize 为 `cancelled`（不获取 lease、不预留 iteration）
7. `RunStore.begin(run_id, plan_hash)` — 获取 frozen RunLease（含 `lease_id`）
8. **R5 P0-5 / R6 P0-5** `get_usage_capabilities(invoker)` — 一次性冻结 Invoker Usage Capability。**R6 P0-5**: 此步骤在 Identity Probe（步骤 2）**之后**执行，确保 cache hit 路径不读取 Live Invoker
9. `ExecutionUsage` 初始化为 0

任一检查失败 → 抛 `SupervisorError`，不调用任何 Handler。

> **R3 P0-1 取消不得使无效 Plan 变成合法缓存**：取消检查移到 Registry/Validator/Binding 之后。一个 Hash 自洽但 Registry 过期 / Handler 不存在 / Validator 失败的 Plan，即使在调用时 Run 已被取消，也**不会**被缓存为 `cancelled` 结果——Pre-flight 先拒绝，再考虑取消路径。缓存命中的 Completed Result 仍可绕过 live Registry 漂移（这是预期的幂等行为）。

> **R6 P0-5 Cache Path Isolation**：`get_usage_capabilities(invoker)` 在 Identity Probe **之后**执行。Cache hit 路径（步骤 2 返回 `cached_result`）不读取任何 Live Invoker 状态——`invoker.usage_capabilities` property 可能 raise 或有副作用，但 cache hit 不受影响。详见 §8 R6 P0-5。

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

**总调用次数** = `1 + RetryPolicy.max_retries`（R6: 从 `PlannedTask.retry_policy` 读取，而非 `task.max_retries`）

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

### R6 P0-2: should_retry() Pure Function

R5 将 `RetryPolicy` 提升为 Canonical Planning Contract，但运行时的 retry 决策仍散落在 `_execute_task()` 的内联逻辑中，且读取 `task.max_retries` 而非 `PlannedTask.retry_policy`——这意味着 R5 测试篡改 `task.max_retries` 就能影响 retry 行为，而 `RetryPolicy.max_retries` 和 `retryable_error_codes` 两个字段实际上都没有被读取。一个字段进入了 Plan Hash 但不影响执行的 contract 是"审计外壳"。

R6 提取纯函数 `should_retry()`，作为 retry 决策的唯一入口：

```python
def should_retry(
    *,
    policy: RetryPolicy,
    attempt_index: int,
    error_code: str | None,
    explicitly_retryable: bool,
) -> bool: ...
```

**决策规则**（按优先级）：

1. `policy.max_retries <= 0` → `False`（永不重试）
2. `attempt_index >= policy.max_retries` → `False`（预算耗尽）
3. `error_code in NEVER_RETRYABLE_ERROR_CODES` → `False`（**始终拒绝**，即使该 code 出现在 `retryable_error_codes` 中）
4. `policy.retryable_error_codes` 非空 → `error_code` 必须在集合中（且不在 never-retry 列表）
5. `policy.retryable_error_codes` 为空 → 仅重试默认安全类别（`task_timeout`、`retryable_error`）或 `explicitly_retryable=True`

**关键约束**：

- `_execute_task()` 的 retry loop 调用 `should_retry(policy=pt.retry_policy, ...)`，**不**再读取 `task.max_retries`
- `retry_policies` 字典在 `execute()` 入口构建：`{pt.task.task_id: pt.retry_policy for pt in plan.tasks}`
- `NEVER_RETRYABLE_ERROR_CODES` 定义在 `planning.py`，被 planning-layer validator 和 runtime `should_retry()` 共享——确保 Plan Hash 中的 `retryable_error_codes` 和运行时拒绝列表使用同一份规范

```python
# planning.py
NEVER_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset({
    "invalid_receipt", "invalid_result", "usage_unavailable",
    "non_retryable_error", "run_deadline_exceeded",
    "tenant_mismatch", "agent_identity_mismatch",
    "cancelled", "kill_switch",
})
```

### R6 P1: RetryPolicy.retryable_error_codes 内容校验

R5 的 `RetryPolicy` 接受任意 `frozenset[str]` 作为 `retryable_error_codes`——空字符串、纯空白、拼写错误、甚至 `NEVER_RETRYABLE_ERROR_CODES` 中的 code 都能进入 Plan Hash。这些无效配置在运行时会被 `should_retry()` 静默忽略，但已经污染了 Plan Hash，使两个语义不同的 RetryPolicy 可能产生相同的 hash。

R6 在 `RetryPolicy` 上添加 `field_validator`：

```python
@field_validator("retryable_error_codes")
@classmethod
def _validate_retryable_error_codes(cls, v: frozenset[str]) -> frozenset[str]:
    # 1. strip 空白，拒绝空字符串
    # 2. 拒绝 NEVER_RETRYABLE_ERROR_CODES 中的 code（运行时始终拒绝）
    ...
```

**保证**：进入 Plan Hash 的每个 `retryable_error_code` 都是 stripped 非空字符串，且不在 never-retry 列表中——misconfiguration 在 planning 时被发现，而非运行时静默忽略。

### R7 P0-4: Multi-error Retry — code and flag from the SAME AgentError

R6 的 `should_retry()` 接受分离的 `error_code: str | None` + `explicitly_retryable: bool`。但 `_execute_task()` 在处理多错误 `AgentResult` 时构造这两个参数来自**不同**的 Error：

```python
result_retryable = any(err.retryable for err in result.errors)
error_code = result.errors[0].error_code  # 来自 errors[0]
explicitly_retryable = result_retryable     # 来自任意 error
```

如果 Error 1 (`custom_error`, `retryable=False`) 在 allowlist 中，Error 2 (`other_error`, `retryable=True`) 不在 allowlist 中——R6 会传入 `error_code="custom_error"` + `explicitly_retryable=True`，因 `custom_error` 在 allowlist 中而重试，但 `custom_error` 本身标记为不可重试；真正被标为 retryable 的 `other_error` 又不在 allowlist 中。

R7 提取 `should_retry_result()`，接受 `Sequence[AgentError]`：

```python
def should_retry_result(
    *,
    policy: RetryPolicy,
    attempt_index: int,
    errors: Sequence[AgentError],
) -> bool: ...
```

**决策规则**（按优先级）：

1. `policy.max_retries <= 0` → `False`
2. `attempt_index >= policy.max_retries` → `False`
3. 无 errors → `False`
4. **任一** error 的 `error_code` 在 `NEVER_RETRYABLE_ERROR_CODES` 中 → `False`（始终拒绝）
5. 过滤出 `retryable=True` 的 errors（code + retryable 来自**同一个** AgentError）
6. 无 retryable errors → `False`
7. `policy.retryable_error_codes` 非空 → 至少一个 retryable error 的 `error_code` 在集合中
8. `policy.retryable_error_codes` 为空 → 至少一个 retryable error 即可

**关键约束**：`error_code` 与 `retryable` 始终来自同一个 `AgentError` 实例——不允许把 Error A 的 code 与 Error B 的 retryable flag 拼接。`_execute_task()` 的 3 个 retry 调用点（结果失败、超时、异常）都改为构造 `AgentError` 列表并调用 `should_retry_result()`。

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

### R6 P0-3: No-Receipt Attempt Fail-Closed + Per-Dimension Skip

R5 的 fail-closed 只覆盖"Receipt 有但 usage 字段为 None"的场景。但一个 committed agent call 可能因为 Timeout、Handler 异常、或结构化并发取消而**根本不产生 Receipt**——此时 `record_receipt()` 从未被调用，`_usage_unavailable` flag 永远不会被设置。如果 `token_budget` 或 `cost_budget_usd` 已配置，系统会静默地将"未知消耗"当作"零消耗"继续调度后续任务。

R6 引入 `record_usage_unavailable()`，在每次 committed agent call 未产生 Receipt 时调用：

```python
def record_usage_unavailable(self) -> None:
    self._provider_usage_capable_attempts += 1
    if (self._budget.token_budget is not None
            or self._budget.cost_budget_usd is not None):
        self._usage_unavailable = True
        self._exceeded = True
        self._exceeded_reason = "execution_usage_unavailable"
```

**Usage Disposition 三态**（每个 committed agent call 必须产生且仅产生一个）：

| Disposition | 触发条件 | 处理 |
|---|---|---|
| `verified` | Receipt 且 `tokens_verified` / `cost_verified` | `record_receipt()` 累计并 enforce |
| `no_provider_call` | Receipt 但 `provider_metadata is None`（deterministic mode） | `record_receipt()` 跳过 enforcement |
| `unavailable` | 无 Receipt（timeout / exception / cancel） | `record_usage_unavailable()` → fail-closed |

#### Per-Dimension Enforcement Skip（三条件）

`record_receipt()` 对 Token 和 Cost **分别**执行 verify → record → enforce。enforcement 仅在以下**三个条件同时满足**时跳过：

```
provider_metadata is None          (无 provider 调用)
AND tokens_used / cost_usd is None (无 usage 报告)
AND not verified                   (该维度未被验证)
```

这确保 deterministic-mode receipt（无 `provider_metadata`、无 usage、未验证）不触发 fail-closed，而以下场景**仍然** fail-closed：

| `provider_metadata` | `tokens_used` / `cost_usd` | `verified` | 行为 |
|---|---|---|---|
| `None` | `None` | `False` | **skip**（deterministic mode） |
| `None` | `0` / `Decimal("0")` | `False` | **fail-closed**（有值但未验证） |
| set | `None` | `False` | **fail-closed**（provider 调用了但未报告） |
| set | `500` | `False` | **fail-closed**（有值但未验证） |
| set | `500` | `True` | **enforce**（累计检查上限） |

**保证**：`ExecutionUsageUnavailableError` 被捕获后设置 `error_code='usage_unavailable'`，同时 `_exceeded=True` + `_exceeded_reason='execution_usage_unavailable'`——run 最终状态为 `BUDGET_EXCEEDED`（而非 `FAILED`），因为 budget 配置了但无法测量消耗是一种 budget 级别的 fail-closed。

### R7 P0-1: Trusted No-provider-call — Handler Cannot Self-attest

R6 的 `no_provider_call` disposition 仍由 Handler 通过"省略 `provider_metadata`"自行声明——一个实际调用过 Provider 的 Handler 只要漏填或故意不填 `provider_metadata`，就会被当成 Deterministic、No-provider-call Attempt，从而跳过 Token/Cost Budget 校验。

R7 引入**显式且可信的** `AttemptUsageDisposition`（per-dimension）：

```python
class AttemptUsageDisposition(StrEnum):
    VERIFIED = "verified"
    NO_PROVIDER_CALL = "no_provider_call"
    UNAVAILABLE = "unavailable"
```

`UsageVerificationCapabilities` 新增 `can_attest_no_provider_call: bool`。只有受信 Deterministic Invoker 或 Runtime Mode Adapter 可设为 `True`——默认 `RegistryAgentInvoker` 为 `False`（一个真实 Handler 可以通过省略 `provider_metadata` 说谎）。

**`record_receipt()` 中的 disposition 计算规则**（per-dimension）：

```
if provenance.{dim}_verified:
    disposition = VERIFIED
elif can_attest_no_provider_call AND provider_metadata is None:
    disposition = NO_PROVIDER_CALL
else:
    disposition = UNAVAILABLE
```

**关键变化**：`provider_metadata is None` **不再**自动产生 `NO_PROVIDER_CALL`。当 `can_attest_no_provider_call=False`（默认 `RegistryAgentInvoker`）且 `provider_metadata` 缺失时，disposition 是 `UNAVAILABLE`——配置 Token/Cost Budget 时必须 fail-closed。

| `provider_metadata` | `can_attest_no_provider_call` | `provenance.verified` | disposition |
|---|---|---|---|
| None | `True` (deterministic) | False | **NO_PROVIDER_CALL** |
| None | `False` (registry) | False | **UNAVAILABLE** (fail-closed if budget) |
| set | any | True | **VERIFIED** |
| set | any | False | **UNAVAILABLE** (fail-closed if budget) |

### R7 P0-2: Invalid Receipt also Produces Usage Disposition

R6 的 `record_usage_unavailable()` 只位于"原本就没有 receipt"分支中。如果一个已 commit 的 Agent Call 返回了无效 Receipt（`validate_invocation_receipt()` 失败），R6 仅标记 `invalid_receipt` 并继续，但**不**调用 `record_usage_unavailable()`——Token/Cost Usage 不会标记为 Unavailable，配置预算时也不会触发 `execution_usage_unavailable`，独立任务仍可能继续执行。

R7 在 invalid-receipt 路径也调用 `record_usage_unavailable()`：

```python
# observed tool calls charged BEFORE receipt validation
accountant.record_observed_tool_calls(observed_tool_calls)

try:
    validate_invocation_receipt(receipt)
except InvalidInvocationReceiptError:
    receipt_for_record = receipt
    receipt = None
    # R7 P0-2: invalid receipt → both dimensions UNAVAILABLE
    accountant.record_usage_unavailable(task_id=task.task_id, attempt=attempt_idx)
```

**保证**：invalid receipt 时 observed tool calls 仍计入 `max_tool_calls`，但 Token 和 Cost disposition 均为 `UNAVAILABLE`——配置对应预算时触发 `execution_usage_unavailable`，停止 Retry、停止新任务、Run=budget_exceeded。

### R7 P0-3: Per-Dimension Independent Coverage Denominators

R6 的 `provider_usage_capable_attempts` 是 Token 和 Cost 共享的 coverage 分母。一个 Cost-only Adapter 验证 Attempt B 的 Cost 可以错误地"抵消"Attempt A 缺失的 Cost——最终 cost_usage_status 可能被错误标记为 `COMPLETE`。

R7 为 Token 和 Cost 使用**独立分母**：

```python
token_usage_applicable_attempts: int   # R7 P0-3: per-dimension denominator
cost_usage_applicable_attempts: int    # R7 P0-3: per-dimension denominator
verified_token_attempts: int
verified_cost_attempts: int
# DEPRECATED (R7): retained only as a diagnostic; computed as
# max(token_usage_applicable_attempts, cost_usage_applicable_attempts)
provider_usage_capable_attempts: int
```

并引入 per-attempt `AttemptUsageRecord`：

```python
class AttemptUsageRecord(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    attempt: int
    token_disposition: AttemptUsageDisposition
    cost_disposition: AttemptUsageDisposition
    tokens_used: int | None = None
    cost_usd: Decimal | None = None
    source_id: str | None = None
```

`_BudgetAccountant` 维护 `_attempt_records: list[AttemptUsageRecord]`，per-dimension coverage 从记录计算：

```
token_usage_applicable_attempts = count(r for r in records if r.token_disposition != NO_PROVIDER_CALL)
cost_usage_applicable_attempts  = count(r for r in records if r.cost_disposition != NO_PROVIDER_CALL)
verified_token_attempts         = count(r for r in records if r.token_disposition == VERIFIED)
verified_cost_attempts          = count(r for r in records if r.cost_disposition == VERIFIED)
```

**保证**：`verified_token_attempts <= token_usage_applicable_attempts` 且 `verified_cost_attempts <= cost_usage_applicable_attempts`——一个 Cost-only Adapter 不得补齐另一个 Provider Attempt 缺失的 Cost。

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

### R6 P0-4: Per-Dimension Usage Provenance

R3–R5 的 `usage_trust: UsageTrustLevel` 是单一字符串，将 Token 和 Cost trust 混为一体。但现实中的 Verifier 可能只验证 Token（Provider API 返回 token count 但不含 cost）、或只验证 Cost（本地计费系统签名 cost 但不报告 token）。单一 `usage_trust` 无法表达"Token 已验证但 Cost 未验证"——要么两者都 `trusted_adapter`，要么都 `unverified`。

R6 引入 per-dimension `UsageProvenance`：

```python
class UsageProvenance(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = "unverified"
    tokens_verified: bool = False
    cost_verified: bool = False
```

`AgentInvocationReceipt` 新增 `usage_provenance: UsageProvenance` 字段。旧的 `usage_trust` 字段保留（auto-derived from provenance），通过 `model_validator(mode="before")` 双向同步：

- 只传 `usage_trust` → 从 `_TRUST_TO_PROVENANCE` 映射推导 `usage_provenance`
- 只传 `usage_provenance` → 从 `_provenance_to_trust()` 推导 `usage_trust`
- 两者都传 → `usage_provenance` 优先

#### `_TRUST_TO_PROVENANCE` 映射（R6 修正）

```python
_TRUST_TO_PROVENANCE = {
    "verified_provider": UsageProvenance(
        source_id="verified_provider",
        tokens_verified=True,
        cost_verified=False,   # Provider 返回 token count，不直接报告 cost
    ),
    "trusted_adapter": UsageProvenance(
        source_id="trusted_adapter",
        tokens_verified=False,  # R6: trusted_adapter 是 cost trust，不自动验证 tokens
        cost_verified=True,
    ),
    "unverified": UsageProvenance(
        source_id="unverified",
        tokens_verified=False,
        cost_verified=False,
    ),
}
```

**关键语义修正**：`trusted_adapter` 从 R5 的 `{tokens_verified=True, cost_verified=True}` 改为 `{tokens_verified=False, cost_verified=True}`。`trusted_adapter` 是关于 **cost** 信任（如本地计费系统签名 cost 报告），**不**自动提升 token 信任——token 由 LLM provider（`verified_provider`）或显式 `ProviderUsageVerifier` 实证。

#### `_provenance_to_trust()` 反向映射

```python
def _provenance_to_trust(prov: UsageProvenance) -> UsageTrustLevel:
    if prov.cost_verified:
        return "trusted_adapter"     # cost 已验证 → trusted_adapter
    if prov.tokens_verified:
        return "verified_provider"   # 只有 token 已验证 → verified_provider
    return "unverified"
```

#### `validate_invocation_receipt()` per-dimension 校验

- `tokens_verified=True` 要求 `provider_metadata` 非空（token 信任需要 provider 上下文）
- `cost_verified=True` **不**要求 `provider_metadata`（本地计费系统可以独立签名 cost）

#### `_BudgetAccountant.record_receipt()` per-dimension 流程

对 Token 和 Cost **分别**执行 verify → record → enforce：

```
Token:  if tokens_verified and tokens_used is not None → 累计
        if token_budget configured and not skip → enforce
Cost:   if cost_verified and cost_usd is not None → 累计
        if cost_budget configured and not skip → enforce
```

一个 Verifier 只验证 Token 的场景：`tokens_verified=True, cost_verified=False` → Accountant 记录 tokens 但**不**记录 cost，`cost_budget_usd` enforcement 仍会 fail-closed（如果配置了）。

### R4 P0-2 / R5 P0-5: Authoritative Provider Usage Verification

R3 的 `TrustedUsageInvoker` 标记 Protocol 是**可伪造**的——任意自定义 Invoker 可以直接在 Receipt 中设置 `usage_trust="trusted_adapter"` 而无需实际实现 `usage_is_verified=True`。R4 用 `UsageVerificationCapabilities` 替代 marker Protocol。

**R5 P0-5 进一步发现**：默认 `RegistryAgentInvoker` 仍可通过 Handler 自报 Metadata 获得 Token Trust。其可信判断仍依赖 Handler 返回的 `AgentResult.provider_metadata` 和 `AgentResult.token_usage`——只要 `provider_metadata` 非空，就可能将 Receipt 标记为 `verified_provider`。但这些字段本身仍由 Handler 构造，而不是由独立 Provider Adapter 或可信计费边界生成。

R5 引入**权威 Provider Usage Verifier** Protocol：

```python
class ProviderUsageVerifier(Protocol):
    source_id: str

    def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage: ...

class VerifiedUsage(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    verified: bool = False
```

**R6 P0-1 签名变化**：`verify()` 接受 `provider_metadata` + `token_usage`（而非 R5 文档中的 `source_id` + `provider_receipt`），返回的 `VerifiedUsage` 新增 `verified: bool` 字段和 `cost_usd: Decimal | None`（可为 None，表示 Verifier 只验证了 token）。`source_id` 移到 Protocol 属性上。

**默认 `RegistryAgentInvoker` 行为**（无 `usage_verifier` 参数时）：

```
verifies_tokens = False
verifies_cost = False
```

**只有**显式配置了权威 `ProviderUsageVerifier` Adapter 时，`RegistryAgentInvoker` 才声明能够验证 Provider Usage。Trust 来源**不**得由 `AgentResult.provider_metadata is not None` 推导。

### R6 P0-1: Verifier Actually Invoked on Real Path

R5 的 `ProviderUsageVerifier` Protocol 存在但**从未在真实 Invocation 路径中被调用**——`RegistryAgentInvoker.invoke()` 只检查 `self._usage_verifier is not None` 来决定 `usage_capabilities`，但 Handler 返回 `provider_metadata` 后并未调用 `verifier.verify()`。一个配置了 Verifier 的 Invoker 会声明 `verifies_tokens=True`，但 Receipt 中的 token 值仍来自 Handler 自报——"存在 Verifier 对象"被等价于"Usage 已验证"。

R6 在 `RegistryAgentInvoker.invoke()` 的真实路径中调用 Verifier：

```python
async def invoke(self, handler, task, context) -> AgentInvocationReceipt:
    result = await handler.run(task, context)

    # R6 P0-1: Actually call the verifier — its returned `verified`
    # flag (not the verifier's mere existence) determines trust.
    if self._usage_verifier is not None and result.provider_metadata is not None:
        try:
            verified = self._usage_verifier.verify(
                provider_metadata=result.provider_metadata,
                token_usage=result.token_usage,
            )
        except Exception as exc:
            # Verifier raised → fail closed
            raise NonRetryableAgentError(...) from exc

        if not verified.verified:
            # Verifier rejected → fail closed
            raise NonRetryableAgentError(...)

        # Use the verifier's authoritative values, not Handler's self-report
        return AgentInvocationReceipt(
            result=result,
            tokens_used=verified.tokens_used,
            cost_usd=verified.cost_usd,
            usage_provenance=UsageProvenance(
                source_id=self._usage_verifier.source_id,
                tokens_verified=True,
                cost_verified=verified.cost_usd is not None,
            ),
        )

    # No verifier or no provider_metadata → unverified
    ...
```

**关键保证**：

- Verifier **抛异常** → `NonRetryableAgentError`（fail closed，不重试）
- Verifier 返回 `verified=False` → `NonRetryableAgentError`（fail closed）
- Verifier 返回 `verified=True` → 使用 Verifier 的 `tokens_used` / `cost_usd`（**不**使用 Handler 自报值），`cost_verified` 取决于 `verified.cost_usd is not None`
- 无 Verifier 或无 `provider_metadata` → `UsageProvenance(tokens_verified=False, cost_verified=False)`

### R7 P0-5: Async ProviderUsageVerifier Bounded by Deadline

R6 的 `ProviderUsageVerifier.verify()` 是同步方法，但 `RegistryAgentInvoker.invoke()` 在 async 方法中直接同步调用：

```python
verified = self._usage_verifier.verify(...)  # 同步调用阻塞事件循环
```

虽然外层 Invocation 使用 `asyncio.wait_for(invoker.invoke(...))`，但当同步 `verify()` 阻塞线程时：`wait_for` 的 Timeout Callback 无法执行、同波其他 Task 无法调度、Cancellation 无法被轮询、Run Deadline 可以被严重超出。Verifier 的定义允许它是外部、运营或计费 Adapter——不能假定它永远是一个立即返回的纯内存函数。

R7 将 Protocol 改为 `async def`：

```python
class ProviderUsageVerifier(Protocol):
    source_id: str

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage: ...
```

`RegistryAgentInvoker.invoke()` 使用 `await`：

```python
verified = await self._usage_verifier.verify(...)
```

Verifier 调用受**剩余 Task Timeout + 剩余 Run Deadline + Cancellation** 约束——`asyncio.wait_for` 的 Timeout Callback 可以正常执行，同波 Task 可以继续调度，Cancellation 可以被及时轮询。

**关键保证**：
- 慢 Verifier 被 Task Timeout 或 Run Deadline 取消——不会无限运行
- 一个慢 Verifier 不会阻塞同波其他 Task（事件循环不被阻塞）
- `invoke()` 协程被 cancel 时，Verifier 也被 cancel——不会遗留后台 Verifier
- Verifier 超时 → `NonRetryableAgentError`（fail closed）
- 如果必须兼容同步 Adapter，应通过专用 Adapter 包装（如 `asyncio.to_thread`），并明确其线程生命周期；**不**得在事件循环中直接执行潜在阻塞代码

### R7 P0-6: Provenance Source Binding

R6 的 `UsageVerificationCapabilities` 只检查 `verifies_tokens` / `verifies_cost` 两个 Boolean——一个 Invoker 声明 `verifies_tokens=True` 后，任何 `source_id` 的 Receipt 都会被接受。但 Invoker 应该绑定到**特定** Verifier/Adapter 的 source identity，而不是接受任意 source。

R7 在 `UsageVerificationCapabilities` 新增 `bound_source_ids: frozenset[str]`：

```python
class UsageVerificationCapabilities(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str
    can_attest_no_provider_call: bool = False  # R7 P0-1
    bound_source_ids: frozenset[str] = Field(default_factory=frozenset)  # R7 P0-6
```

`record_receipt()` 在处理 Receipt 时执行 source binding 检查：

```python
if invoker_capabilities.bound_source_ids:
    if prov.source_id not in invoker_capabilities.bound_source_ids:
        raise ExecutionUsageUnavailableError(
            f"receipt.usage_provenance.source_id={prov.source_id!r} "
            f"is not in the invoker's bound_source_ids — "
            f"receipt cannot claim provenance from an unbound source"
        )
```

**关键保证**：
- `bound_source_ids` 非空 → Receipt 的 `source_id` 必须在集合中
- `bound_source_ids` 为空 → 任何 `source_id` 接受（向后兼容）
- `RegistryAgentInvoker` 配置 Verifier 后，`bound_source_ids = frozenset({verifier.source_id})`
- `DeterministicFakeInvoker` 默认 `bound_source_ids=frozenset()`（不绑定特定 source）
- 一个 Receipt 不能声称由 Invoker 未绑定的 Verifier 验证

**Trust 来源**是 Verifier 的 `verify()` 返回值，**不**是 Verifier 对象的存在性。

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

#### R6 P0-5: Cache Path Isolation

R5 的 Pre-flight 顺序中，`get_usage_capabilities(invoker)` 在 Identity Probe **之前**执行。这意味着一个 completed run 的 cache hit 路径（`lookup_run_identity` 返回 `status="completed"` + `plan_hash_matches=True` → 直接返回 `cached_result`）仍会读取 Live Invoker 的 `usage_capabilities` property。如果 Invoker 是可变对象、或其 property 实现有副作用（如网络调用）、或此时 Invoker 尚未初始化，cache hit 路径会失败或产生意外行为——而 cache hit 应该是**纯只读**操作。

R6 将 `get_usage_capabilities(invoker)` 移到 Identity Probe **之后**：

```
Correct order (R6):
  1. plan.verify_integrity()              (no side effects)
  2. RunStore identity probe              (read-only)
       - same run + same plan + completed  → cache hit, return  ← NO invoker read
       - same run + different plan          → RunPlanConflictError
       - same run + same plan + running     → RunAlreadyInProgressError
  3. Registry Version                      (no side effects)
  4. PlanValidator                         (no side effects)
  5. Execution Bindings (handler resolve)  (no side effects)
  6. Cancellation / Kill Switch            (read-only)
  7. RunStore.begin()                      (mutates store)
  8. Freeze Usage Capabilities             (read invoker ONCE, after cache miss)
  9. Dispatch or finalize as cancelled
```

**保证**：Cache hit 路径**不**读取任何 Live Invoker 状态——`invoker.usage_capabilities` property 可能 raise 或有副作用，但 cache hit 不受影响。Usage Capability 冻结只在确认需要执行（cache miss + pre-flight 通过）后才发生。

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

### R6 P1: Three-State UsageAvailabilityStatus

R5 的 `ExecutionUsage` 使用 `tokens_usage_available: bool` / `cost_usage_available: bool` 表示 usage 可用性。布尔值只能表达"至少一次 receipt 报告了 verified usage"——无法区分"所有 provider-usage-capable attempt 都已验证"（COMPLETE）和"部分验证、部分未验证"（PARTIAL）。一个 5-task run 中 3 个 task 有 verified receipt、2 个 timeout 无 receipt，R5 报告 `tokens_usage_available=True`，掩盖了 40% 的 usage 不可用。

R6 升级为三态 `UsageAvailabilityStatus`：

```python
class UsageAvailabilityStatus(StrEnum):
    UNAVAILABLE = "unavailable"  # 无 committed attempt 产生 verified usage
    PARTIAL     = "partial"      # 至少一个 verified，但非全部 capable attempt
    COMPLETE    = "complete"     # 全部 capable attempt 都 verified（或无 capable attempt）
```

`ExecutionUsage` 新增 `tokens_usage_status` / `cost_usage_status` 字段，旧的 boolean 字段保留（auto-derived：`available = status != UNAVAILABLE`）：

```python
class ExecutionUsage(StrictContract):
    ...
    tokens_usage_available: bool = False                    # legacy, auto-derived
    tokens_usage_status: UsageAvailabilityStatus = UNAVAILABLE  # R6 P1
    cost_usage_available: bool = False                      # legacy, auto-derived
    cost_usage_status: UsageAvailabilityStatus = UNAVAILABLE    # R6 P1
    provider_usage_capable_attempts: int = 0                # R6: committed calls with provider_metadata
    verified_token_attempts: int = 0                        # R6: verified token receipts
    verified_cost_attempts: int = 0                         # R6: verified cost receipts
```

`_compute_usage_status()` 计算逻辑：

```python
def _compute_usage_status(self, verified: int, capable: int) -> UsageAvailabilityStatus:
    if capable == 0:           return COMPLETE    # 无 capable attempt → 全部"验证"
    if verified == capable:    return COMPLETE    # 全部 verified
    if verified > 0:           return PARTIAL     # 部分 verified
    return UNAVAILABLE                             # 无 verified
```

**保证**：`PARTIAL` 状态明确告知调用方"部分 usage 数据缺失"，调用方可以据此决定是否信任 `tokens_used` / `cost_usd` 的累计值，而非将其当作完整测量。

### R7 P1-1: Legacy usage_trust / UsageTrustLevel DEPRECATED

R6 的 `AgentInvocationReceipt` 仍公开 `usage_trust: UsageTrustLevel` 并允许旧字符串自动生成新的 `UsageProvenance`。`multi_agent.__init__` 也仍导出了 `UsageTrustLevel`。Capability 交叉校验能阻止部分自我提权，但保留两套公共 Trust API 会使调用者难以判断哪一个才是正式模型。

R7 将 `usage_trust` / `UsageTrustLevel` 标记为 **DEPRECATED**：

- Runtime 内部（`_BudgetAccountant`）**只**读取 `usage_provenance`，**不**读取 `usage_trust`
- `usage_trust` 字段保留但 auto-derived from `usage_provenance`（通过 `_provenance_to_trust()`）
- 禁止新代码同时传入两套字段——`_sync_trust_provenance` validator 让 `usage_provenance` 优先，但混用易错
- `multi_agent.__init__` 中的 `UsageTrustLevel` 导出标注 DEPRECATED 注释
- 新代码**必须**使用 `UsageProvenance` 和 `AttemptUsageDisposition`
- 下一次不兼容版本将删除旧字段与导出

**推荐迁移**：

```python
# DEPRECATED (R7):
receipt = AgentInvocationReceipt(
    result=result,
    usage_trust="verified_provider",  # 旧 API
)

# RECOMMENDED (R7+):
receipt = AgentInvocationReceipt(
    result=result,
    usage_provenance=UsageProvenance(
        source_id="provider_verifier",
        tokens_verified=True,
        cost_verified=False,
    ),
)
```

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
7. **R5 P0-5 / R6 P0-1 ProviderUsageVerifier 默认未配置** — 默认 `RegistryAgentInvoker` 不声明能够验证 Provider Usage（`verifies_tokens=False, verifies_cost=False`）。生产部署需要显式配置权威 Provider Usage Adapter——R6 P0-1 确保配置后 `verify()` 会在真实 `invoke()` 路径被调用，其返回值（而非 Verifier 对象存在性）决定 trust。
8. **R6 P0-4 trusted_adapter 语义收窄** — R6 将 `trusted_adapter` 从"token+cost 都验证"收窄为"仅 cost 验证"（`tokens_verified=False`）。依赖旧语义（`trusted_adapter` receipt 自动获得 token trust）的部署需要显式配置同时验证 token 的 `ProviderUsageVerifier`。

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
├── test_supervisor_r5.py                 # R5 regression (P0-1..P0-5, P1-1..P1-2) — 22 tests
├── test_supervisor_r6.py                 # R6 regression (P0-1..P0-5, P1-1..P1-2) — verified usage flow + retry policy execution
└── test_supervisor_r7.py                 # R7 regression (P0-1..P0-6, P1-1) — trusted no-provider-call + per-dimension coverage + multi-error retry + async verifier + source binding — 33 tests
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

R6 反例测试（`test_supervisor_r6.py`）覆盖：
- **P0-1** Verifier Actually Invoked — 配置 verifier 后 `invoke()` 真实调用 `verify()`；verifier 抛异常 → `NonRetryableAgentError`；verifier 返回 `verified=False` → `NonRetryableAgentError`；verifier 返回 `verified=True` 使用 verifier 值而非 Handler 自报；无 verifier 时 receipt 为 unverified
- **P0-2** should_retry() Pure Function — `RetryPolicy.max_retries=0` 不重试；`retryable_error_codes` 非空时只重试列表中的 code；`NEVER_RETRYABLE_ERROR_CODES` 始终拒绝即使出现在列表中；`retryable_error_codes` 为空时只重试默认安全类别；`should_retry()` 读取 `RetryPolicy` 而非 `task.max_retries`
- **P0-3** No-Receipt Attempt Fail-Closed — committed call 无 receipt 时 `record_usage_unavailable()` 设置 `_exceeded`；budget 配置时 run 终态为 `BUDGET_EXCEEDED`；deterministic-mode receipt（无 `provider_metadata` + 无 usage）skip enforcement；有 `provider_metadata` 但 `cost_usd=None` fail-closed；`cost_usd=0` unverified fail-closed
- **P0-4** Per-Dimension Provenance — `trusted_adapter` 只验证 cost 不验证 token；`verified_provider` 只验证 token 不验证 cost；per-dimension skip 三条件；`UsageProvenance` contract frozen；`usage_trust` 与 `usage_provenance` 双向同步
- **P0-5** Cache Path Isolation — cache hit 路径不读取 `invoker.usage_capabilities`；invoker property 抛异常时 cache hit 仍正常返回；Usage Capability 冻结在 Identity Probe 之后
- **P1-1** Three-State UsageAvailabilityStatus — `UNAVAILABLE` / `PARTIAL` / `COMPLETE` 三态计算；`capable==0` → `COMPLETE`；`verified==capable` → `COMPLETE`；`verified>0` → `PARTIAL`；legacy boolean auto-derived
- **P1-2** retryable_error_codes Validation — 空字符串被拒绝；纯空白被拒绝；`NEVER_RETRYABLE_ERROR_CODES` 中的 code 被拒绝；stripped 非空字符串通过

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

# R5 P0-5 / R6 P0-1: Authoritative Provider Usage Verifier
# R6 P0-1: verify() is actually called on the real invoke() path.
# R6 P0-1: signature changed to (provider_metadata, token_usage).
class ProviderUsageVerifier(Protocol):
    source_id: str  # R6: moved from verify() param to Protocol attribute

    def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,  # R6: was source_id
        token_usage: TokenUsage,               # R6: was provider_receipt
    ) -> VerifiedUsage: ...

class VerifiedUsage(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)  # R6: None = cost not verified
    verified: bool = False  # R6: explicit accept/reject flag

# R6 P0-4: Per-dimension Usage Provenance (replaces single usage_trust)
class UsageProvenance(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = "unverified"
    tokens_verified: bool = False
    cost_verified: bool = False

# R6 P1: Three-state usage availability (replaces boolean flags)
class UsageAvailabilityStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    PARTIAL     = "partial"
    COMPLETE    = "complete"

# R6 P0-2: Pure retry decision function (reads RetryPolicy, not task.max_retries)
def should_retry(
    *,
    policy: RetryPolicy,
    attempt_index: int,
    error_code: str | None,
    explicitly_retryable: bool,
) -> bool: ...

# R6 P0-2 / P1: Canonical never-retryable codes (shared by planning + runtime)
NEVER_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset({
    "invalid_receipt", "invalid_result", "usage_unavailable",
    "non_retryable_error", "run_deadline_exceeded",
    "tenant_mismatch", "agent_identity_mismatch",
    "cancelled", "kill_switch",
})
```

### R7 新增 Contract 速查

```python
# R7 P0-1: Per-dimension Attempt Usage Disposition (replaces R6 heuristic)
class AttemptUsageDisposition(StrEnum):
    VERIFIED = "verified"
    NO_PROVIDER_CALL = "no_provider_call"
    UNAVAILABLE = "unavailable"

# R7 P0-3: Per-attempt usage record (independent Token/Cost coverage)
class AttemptUsageRecord(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    attempt: int
    token_disposition: AttemptUsageDisposition
    cost_disposition: AttemptUsageDisposition
    tokens_used: int | None = None
    cost_usd: Decimal | None = None
    source_id: str | None = None

# R7 P0-1 / P0-6: UsageVerificationCapabilities extended
class UsageVerificationCapabilities(StrictContract):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verifies_tokens: bool = False
    verifies_cost: bool = False
    source_id: str
    can_attest_no_provider_call: bool = False  # R7 P0-1
    bound_source_ids: frozenset[str] = Field(default_factory=frozenset)  # R7 P0-6

# R7 P0-3: ExecutionUsage with per-dimension denominators
class ExecutionUsage(StrictContract):
    ...
    # R7 P0-3: independent per-dimension denominators
    token_usage_applicable_attempts: int = Field(default=0, ge=0)
    cost_usage_applicable_attempts: int = Field(default=0, ge=0)
    verified_token_attempts: int = Field(default=0, ge=0)
    verified_cost_attempts: int = Field(default=0, ge=0)
    # DEPRECATED (R7): retained as diagnostic only
    provider_usage_capable_attempts: int = Field(default=0, ge=0)

# R7 P0-4: Multi-error retry function (code + retryable from same AgentError)
def should_retry_result(
    *,
    policy: RetryPolicy,
    attempt_index: int,
    errors: Sequence[AgentError],
) -> bool: ...

# R7 P0-5: Async ProviderUsageVerifier (bounded by asyncio.wait_for)
class ProviderUsageVerifier(Protocol):
    source_id: str

    async def verify(
        self,
        *,
        provider_metadata: ProviderMetadata,
        token_usage: TokenUsage,
    ) -> VerifiedUsage: ...
```
