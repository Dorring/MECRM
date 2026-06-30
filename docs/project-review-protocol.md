# M-Agent-ECRM 两轮独立模型审查协议

> 目的：让另外的模型对优化计划和后续实现进行两次有明确分工、可追踪证据和阻断规则的审查。  
> 被审查计划：`docs/project-optimization-plan.md`

## 1. 审查原则

1. 审查必须以仓库证据为准，不能仅根据 README、CLAUDE.md 或计划中的声明打分。
2. 审查模型不得直接修改代码；先输出问题、证据和建议，由实现者修订。
3. 第一轮关注“方向是否正确、是否遗漏重大风险”。
4. 第二轮在修订后重新检查“问题是否真实关闭、是否引入回归”，不能只复述第一轮。
5. 两轮建议使用不同模型或至少使用全新上下文。
6. 每个问题必须包含文件、行号或命令输出；无法证明的问题标为“待验证”，不能标为事实。
7. P0/P1 问题不得通过文字解释关闭，必须有实现或验证证据。

## 2. 角色分工

| 角色 | 职责 |
|---|---|
| Implementer | 实现任务、填写自审查、提供验证证据 |
| Review Model 1 | 架构、安全、边界和计划完整性审查 |
| Remediation Owner | 处理第一轮问题并维护 disposition |
| Review Model 2 | 独立复验、回归、可运行性和证据审查 |
| Release Owner | 确认门禁，接受或拒绝剩余风险 |

审查模型不能同时担任对应轮次的实现者。

## 3. 审查输入包

每轮开始前提供以下材料：

```text
1. 当前 commit/version 标识
2. AGENTS.md
3. CLAUDE.md
4. docs/project-optimization-plan.md
5. 上一轮审查结果（第二轮才提供）
6. 问题修复 disposition（第二轮才提供）
7. 变更文件清单或 diff
8. 实际执行的测试命令和原始结果
9. 未执行测试及原因
10. 已知限制和风险接受记录
```

若没有 Git 元数据，必须提供文件 checksum 或打包版本号，确保两轮审查针对明确版本。

## 4. 严重性定义

| 等级 | 定义 | 处理规则 |
|---|---|---|
| P0 Blocker | 可导致跨租户泄露、认证绕过、不可恢复数据损坏、关键事件丢失或部署完全不可用 | 立即停止发布 |
| P1 High | 生产故障、高风险治理绕过、迁移/恢复不可靠、关键流程无验证 | 生产发布前关闭 |
| P2 Medium | 局部正确性、可维护性、性能、可观测性或测试缺口 | 建立明确修复计划 |
| P3 Low | 文档、命名、轻量重构、开发体验 | 可进入 backlog |
| Note | 不构成缺陷的建议或待确认事项 | 不阻断 |

## 5. 第一轮审查：架构与风险完整性

### 5.1 目标

第一轮不以“代码能编译”为主要目标，而是验证：

- 计划是否解决真实根因。
- 服务边界和数据所有权是否清楚。
- 同步与异步调用是否合理。
- CQRS、Event Store、Outbox 和 Replay 是否形成闭环。
- 租户隔离、OPA、Agent 治理是否存在绕过。
- 是否遗漏部署、迁移、观测、灾备和运维风险。

### 5.2 必查清单

#### A. 架构边界

- [ ] Gateway 是否仍承担领域写模型、Projector、Agent 推理等过多职责。
- [ ] 每张核心表和每类事件是否有唯一 owner。
- [ ] 是否存在 Gateway、Agents、Core Services 互相直接访问对方私有数据。
- [ ] 同步调用失败是否会形成级联故障。
- [ ] 是否产生“逻辑微服务、物理分布式单体”。

#### B. 数据一致性

- [ ] 所有关键写路径是否使用 Event Store + Outbox 原子事务。
- [ ] 是否存在 DB commit 后直接 Kafka publish。
- [ ] 命令是否支持 expected version 和 idempotency key。
- [ ] 消费者是否有数据库级幂等约束。
- [ ] Replay 是否可确定性重建全部关键读模型。
- [ ] Schema version 是否有兼容和 upcaster 策略。

#### C. 租户与安全

- [ ] Auth 错误是否可能被 catch 后继续放行。
- [ ] Redis、OPA、Keycloak 不可用时是否 fail closed。
- [ ] RLS 是否 `FORCE`，应用账号是否可能绕过。
- [ ] `USING` 与 `WITH CHECK` 是否都覆盖。
- [ ] super admin 跨租户能力是否过宽。
- [ ] WebSocket、Kafka、缓存、Replay、GDPR、向量库是否按租户隔离。
- [ ] 是否存在默认 Secret、明文 Secret、日志 Token/PII。

#### D. Agent 治理

- [ ] Kill Switch 是否覆盖所有工具和副作用入口。
- [ ] 高风险动作是否必须审批。
- [ ] 审批后是否重新检查权限、租户和数据版本。
- [ ] 模型输出是否强类型校验。
- [ ] Prompt injection 是否可能改变工具权限。
- [ ] Explainability 是否泄露 chain-of-thought 或敏感输入。

#### E. 数据库与迁移

- [ ] Prisma 和 SQL 迁移是否存在重复或冲突。
- [ ] 空库初始化是否可重复。
- [ ] 升级和回滚是否明确。
- [ ] GDPR 是否覆盖 Event Store、Redis、Weaviate、备份和审计。
- [ ] 索引、唯一约束和外键是否支撑幂等与租户隔离。

#### F. 部署与运维

- [ ] Compose 是否包含完整服务。
- [ ] Helm template 是否实际覆盖 values 中声明的组件。
- [ ] CI 是否引用不存在路径或被条件永久跳过。
- [ ] health/readiness 是否反映真实可服务状态。
- [ ] 每个关键告警是否存在对应 metric 和 runbook。
- [ ] Kafka/DB/Redis/OPA/LLM 故障是否有恢复路径。

#### G. 计划质量

- [ ] 每项任务是否有明确输出、验收、自审查和回滚。
- [ ] P0/P1 顺序是否正确。
- [ ] 是否存在依赖未解决就提前安排下游工作的情况。
- [ ] 是否把“增加文档”误当作“完成实现”。
- [ ] 是否存在无法自动验证的模糊验收标准。

### 5.3 第一轮模型提示词

将下列提示词与审查输入包一同交给 Review Model 1：

```text
你是 M-Agent-ECRM 的第一轮独立架构与安全审查员。

目标：
1. 审查 docs/project-optimization-plan.md 是否覆盖当前仓库的真实问题。
2. 从源码和配置中寻找计划遗漏、错误假设、边界不清和不可执行项。
3. 重点审查 CQRS/Event Store/Outbox、租户 RLS、OPA、认证、Agent 治理、
   数据迁移、Compose/Helm、CI/CD、可观测性、灾备。

规则：
- 只读审查，不修改代码。
- 不接受文档声明作为实现证据。
- 每个 finding 必须给出 severity、证据文件和行号、影响、复现/验证方法、
  建议修复和验收标准。
- 如果无法证明，标记为 Needs Verification。
- 明确指出优化计划中应新增、删除、重排或改写的内容。
- 最后给出 GO / CONDITIONAL GO / NO-GO 结论。

输出严格使用本协议第 5.4 节格式。
```

### 5.4 第一轮输出格式

```markdown
# Review 1 Report

- Reviewer model:
- Reviewed version:
- Date:
- Decision: GO | CONDITIONAL GO | NO-GO

## Executive Summary

## Findings

### R1-001 [P0] 标题
- Area:
- Evidence:
- Why it matters:
- Reproduction/verification:
- Required remediation:
- Acceptance criteria:
- Plan change required:

## Missing Risks

## Incorrect Assumptions

## Dependency/Sequencing Problems

## Module Scores
| Module | Score / 20 | Blocking reason |

## Required Plan Amendments

## Residual Risks
```

## 6. 第一轮整改记录

实现者必须逐项维护 disposition：

```markdown
| Finding | Severity | Disposition | Evidence | Verification | Status |
|---|---|---|---|---|---|
| R1-001 | P0 | Fixed / Accepted / Rejected / Duplicate | file:line or report | command/result | Open/Closed |
```

规则：

- `Fixed`：必须附代码和测试证据。
- `Accepted`：必须有风险负责人、补偿控制和到期日期；P0 不允许接受。
- `Rejected`：必须用源码或运行证据证明 finding 不成立。
- `Duplicate`：必须指向主 finding。
- 关闭 finding 的人不能只写“已修改”，必须提供复验方式。

## 7. 第二轮审查：独立复验与发布认证

### 7.1 前置条件

第二轮只能在以下条件满足后开始：

1. 第一轮所有 P0/P1 均有 disposition。
2. 计划已根据第一轮意见修订。
3. 实现者完成自审查。
4. 必需测试已经实际执行。
5. 提供新的明确版本标识。

### 7.2 目标

第二轮验证：

- 第一轮问题是否真实关闭。
- 修复是否只覆盖表面症状。
- 是否引入新的安全、兼容性和运行回归。
- 测试是否能证明结论，而不是 mock 掉关键行为。
- Compose、CI、迁移和部署是否可重复。
- 是否达到发布门禁。

### 7.3 独立复验清单

#### A. Finding 关闭验证

- [ ] 对每个 R1 finding 重新读取相关源码。
- [ ] 不依赖 disposition 文本判断关闭。
- [ ] 至少抽查一个负向测试和一个故障测试。
- [ ] 检查修复是否影响其他租户、角色、事件版本和部署环境。

#### B. 安全回归

- [ ] revoked/expired/forged Token。
- [ ] OPA/Redis 故障。
- [ ] 跨租户 CRUD、缓存、WebSocket、Replay、Agent memory。
- [ ] super admin 跨租户写。
- [ ] 日志、错误、trace、审计中的敏感信息。
- [ ] Prompt injection 和越权工具参数。

#### C. 一致性回归

- [ ] DB 成功、Kafka 失败。
- [ ] Kafka 重复事件。
- [ ] 消费者处理后、offset 提交前崩溃。
- [ ] 乱序事件和旧 schema。
- [ ] Outbox Publisher 多实例竞争。
- [ ] Replay 与当前读模型 diff。

#### D. 部署与恢复

- [ ] 空数据库 migration。
- [ ] migration 重复运行和升级。
- [ ] Compose cold start。
- [ ] Helm lint/template/install/upgrade/rollback。
- [ ] 缺失依赖时 readiness 为失败。
- [ ] Kafka、PostgreSQL、Redis、OPA 恢复后的自动回稳。

#### E. 测试证据质量

- [ ] 测试没有通过环境变量绕过生产安全逻辑。
- [ ] integration test 使用真实依赖，而非全部 mock。
- [ ] skipped/xfail 有明确理由和 owner。
- [ ] 检查脚本的“通过”代表实际执行，而非文件存在性。
- [ ] 覆盖关键拒绝和故障路径。

### 7.4 第二轮模型提示词

```text
你是 M-Agent-ECRM 的第二轮独立发布审查员。

你需要重新检查仓库，不得默认第一轮 finding 已正确关闭。

目标：
1. 逐项复验 Review 1 的 P0/P1/P2 finding。
2. 检查修复是否引入新回归。
3. 验证测试、迁移、Compose、Helm 和 CI 证据是否真实充分。
4. 对认证、租户隔离、事务 Outbox、消费者幂等、Agent 治理和恢复路径进行
   独立负向审查。

规则：
- 只读审查，不修改代码。
- 优先运行安全、契约、集成和故障测试；无法运行时说明具体阻塞。
- 不把 mock 测试作为真实基础设施验证。
- 每个 reopened/new finding 都必须有文件行号或命令证据。
- 对第一轮每个 finding 给出 VERIFIED CLOSED / REOPENED / NOT VERIFIABLE。
- 最终给出 RELEASE / RELEASE WITH RECORDED RISKS / DO NOT RELEASE。

输出严格使用本协议第 7.5 节格式。
```

### 7.5 第二轮输出格式

```markdown
# Review 2 Release Certification

- Reviewer model:
- Reviewed version:
- Previous reviewed version:
- Date:
- Decision: RELEASE | RELEASE WITH RECORDED RISKS | DO NOT RELEASE

## Verification Environment

## Review 1 Finding Verification
| Finding | Result | Evidence | Notes |
|---|---|---|---|

## New Findings

### R2-001 [P1] 标题
- Area:
- Evidence:
- Impact:
- Required remediation:
- Acceptance criteria:

## Commands Executed
| Command | Result | Artifact |

## Tests Not Executed
| Test | Blocking reason | Risk |

## Security Certification

## Data Consistency Certification

## Deployment and Recovery Certification

## Module Scores
| Module | Score / 20 | Delta from Review 1 | Release blocker |

## Residual Risks
| Risk | Owner | Compensating control | Expiry |

## Final Decision Rationale
```

## 8. 两轮审查退出标准

允许发布必须同时满足：

- Review 2 没有开放 P0/P1。
- Review 1 的 P0/P1 全部 `VERIFIED CLOSED`。
- P2 有 owner、迭代和验收标准。
- 所有必需测试已执行，无无法解释的 skipped。
- Migration、tenant isolation、Outbox fault injection、E2E、security 测试通过。
- Compose 或目标 Helm 环境完成一次 cold start。
- 告警和 runbook 至少完成一次演练。
- Release Owner 签署剩余风险。

以下任一情况必须 `DO NOT RELEASE`：

- 无法确定被审查代码版本。
- 安全测试通过 test-only bypass 绕过真实逻辑。
- 数据库与 Kafka 仍存在关键直接双写。
- RLS/OPA/Token 撤销无法证明 fail closed。
- 迁移不能在空数据库执行。
- CI/Helm 仍引用缺失文件。
- P0/P1 仅通过风险接受关闭。

## 9. 模块审查记录模板

```markdown
# Module Review: <module>

- Version:
- Reviewer:
- Related task IDs:

## Boundary
- Owned data:
- Sync dependencies:
- Async inputs:
- Async outputs:
- Forbidden dependencies:

## Checklist Score
| Dimension | 0-2 | Evidence |
|---|---:|---|
| Boundary/data ownership | | |
| Tenant isolation | | |
| Authentication/authorization | | |
| Contracts | | |
| Consistency/idempotency | | |
| Resilience | | |
| Observability/audit | | |
| Tests | | |
| Deployment/config | | |
| Docs/runbook | | |

## Failure Modes Reviewed

## Findings

## Decision
Production Ready | Conditional | Not Ready
```

## 10. 建议审查顺序

第一轮：

1. Gateway Auth/Tenant/OPA。
2. PostgreSQL/RLS/Migrations。
3. Write API/Event Store/Outbox。
4. Kafka Consumers/Replay。
5. Agents/Governance。
6. Compose/Helm/CI。
7. Frontend。
8. Observability/DR。

第二轮：

1. 复验所有 P0/P1。
2. 执行 tenant isolation 与认证负向测试。
3. 执行 Outbox/consumer 故障测试。
4. 从空环境验证 migration 和启动。
5. 验证关键 E2E。
6. 检查告警/runbook 和恢复证据。
7. 最后评分并给出发布结论。

该顺序先验证平台安全与数据正确性，再验证产品功能，避免在底层门禁不成立时浪费审查成本。

