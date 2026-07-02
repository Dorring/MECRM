# ADR-001: Kafka Topic Initialization Service

> 状态：Implemented
> 日期：2026-07-02
> 作者：Claude Code
> 关联：Batch 1 后续、Iteration 3 前置

## 1. 背景

当前 `docker-compose.yml` 中 Kafka 配置为：

```yaml
KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
```

这带来几个问题：

1. **topic 数量、partition 数、保留策略失控**：首次消费/生产时自动创建的 topic 默认只有 1 个 partition，且无显式保留/压缩策略，与生产预期不一致。
2. **服务启动顺序不可靠**：Gateway 和 Agents 可能在 topic 尚未创建完成时就开始生产/消费，导致首批消息进入错误分区或触发异常。
3. **topic 清单散落**：没有单一 truth source，新开发者不知道系统依赖哪些 topic。
4. **删除后无法恢复**：如果某个 topic 被误删，`auto.create` 会重建，但 partition 数、配置可能已漂移。
5. **CI/生产一致性差**：本地 `auto.create=true` 掩盖了 topic 未显式声明的问题，而生产通常要求显式管理。

本 ADR 提议引入一个独立的 `kafka-init` 一次性服务，在 Gateway/Agents 启动前集中创建并校验所有 topic。

## 2. 决策

**采用独立 `kafka-init` 一次性服务。**

- 在 `docker-compose.yml` 中新增 `kafka-init` 服务（无 profile，随默认栈启动）。
- 复用已有的 `confluentinc/cp-kafka:7.5.0` 镜像，通过挂载脚本 `scripts/kafka-init.sh` 创建所有 topic。
- `gateway`、`agents`、`replay-service` 和 `smoke-test` 的 `depends_on` 增加 `kafka-init: condition: service_completed_successfully`。
- 关闭 `KAFKA_AUTO_CREATE_TOPICS_ENABLE`，强制显式声明。
- topic 清单集中维护在 `scripts/kafka-init.sh` 的 `TOPICS` 数组中；Gateway 的 `gateway/src/services/kafka.ts` 和 Agents 的 `agents/src/orchestrator/config.py` 中的 topic 集合必须是该清单的子集。

不采用以下替代方案：

- **在 Gateway 启动代码里创建 topic**：会把基础设施职责耦合到应用代码，且每个实例启动时都会尝试创建，需额外分布式锁。
- **保留 auto-create + 运行时检查**：无法保证 partition 数和保留策略，CI 与生产行为不一致。
- **Kafka Admin API 在 Python/TypeScript 中创建**：需要额外依赖和权限，且脚本化更轻量。

## 3. 目标 topic 清单

完整清单见 `scripts/kafka-init.sh` 中的 `TOPICS` 数组。按领域分组包括：

- CRM 领域事件：`crm.leads.*`、`crm.deals.*`、`crm.tickets.*`、`crm.customers.*`、`crm.payments.recorded`、`crm.conversations.closed`、`crm.tasks.updated`、`crm.user.activity`、`crm.invoices.updated`
- 审批：`crm.approvals.required`、`crm.approvals.decision`
- Agent 事件：`crm.agents.task-assigned`、`crm.agents.action-proposed`、`crm.agents.action-executed`、`crm.agents.reasoning`、`crm.agents.dlq` 等
- 审计/合规/安全：`crm.audit.events`、`crm.audit.accessed`、`crm.security.events`、`crm.killswitch.activated`、`crm.gdpr.forget`
- 效率：`crm.productivity.signal`、`crm.productivity.action-suggested` 等
- 旅程与预测：`crm.journey.updated`、`crm.analytics.prediction-generated`、`crm.analytics.forecast-requested`
- 自动化：`crm.automation.policy-created`、`crm.automation.executed`、`crm.automation.simulation.*` 等
- 知识库：`crm.knowledge.draft.created`、`crm.knowledge.published`
- 智能模块：`crm.intelligence.user-query`、搜索/语音/数字孪生/DevX 等 topic
- 死信队列：`crm.dlq.leads`、`crm.dlq.agents`、`crm.dlq.approvals`

配置约定：

| 类型 | Partitions | Retention | 说明 |
|---|---|---|---|
| 高容量领域事件 | 6 | 7d | leads/deals/tickets/customers 等 |
| 普通事件 | 3 | 7d | approvals、knowledge、analytics 等 |
| 审计/合规/安全 | 6/3 | 30d | 审计日志保留更长 |
| DLQ | 6/3 | 14d | 死信队列保留更长 |

> dev 单节点 Kafka 下 replication-factor 只能为 1；生产通过 `KAFKA_REPLICATION_FACTOR` 覆盖。

## 4. 运行时拓扑

```text
postgres redis kafka opa
        ↑
   kafka-init (service_completed_successfully)
        ↓
gateway agents replay-service smoke-test
```

- `kafka-init` 只依赖 kafka `service_healthy`。
- `gateway` / `agents` / `replay-service` / `smoke-test` 依赖 `kafka-init` `service_completed_successfully`。
- `KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`。

## 5. 失败语义

- 如果 `kafka-init` 因任何原因失败（broker 未就绪、权限不足、topic 配置冲突、partition 无 leader），容器退出非零。
- `service_completed_successfully` 不会放行下游；Gateway/Agents 不会启动。
- 修复后 `docker compose up -d` 会重新运行 `kafka-init`（幂等：存在则更新配置，不存在则创建）。
- 如果某个 topic 被误删，重启 `kafka-init` 会重建它。

## 6. 幂等与可重复执行

脚本逻辑：

```bash
for spec in "${TOPICS[@]}"; do
  IFS=':' read -r topic partitions retention <<< "$spec"
  if kafka-topics --bootstrap-server "$BROKER" --describe --topic "$topic" >/dev/null 2>&1; then
    kafka-configs --bootstrap-server "$BROKER" --entity-type topics --entity-name "$topic" \
      --alter --add-config "retention.ms=$retention,cleanup.policy=delete"
  else
    kafka-topics --bootstrap-server "$BROKER" --create \
      --topic "$topic" --partitions "$partitions" --replication-factor "$REPLICATION" \
      --config "retention.ms=$retention" --config "cleanup.policy=delete"
  fi
done
```

- 已存在：校验/更新配置。
- 不存在：创建。
- 删除后：重新创建。

创建完成后会等待所有 partition 的 leader 就绪（通过 `kafka-topics --describe` 检查 `Leader: -1/none`），超时则失败。

## 7. 对现有代码的影响

- `docker-compose.yml`：新增 `kafka-init` 服务，关闭 auto-create，调整 `depends_on`。
- `gateway/src/services/kafka.ts`：producer 和 consumer 均设置 `allowAutoTopicCreation: false`。
- `agents/src/orchestrator/config.py`：无需改动，仍通过 `KAFKA_BROKERS` 连接。
- 新增/更新 `scripts/kafka-init.sh`、`scripts/kafka-init.ps1`（PowerShell 等价）。
- 新增/更新 `tests/infra/test_kafka_init.py` 静态测试：
  - `KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`；
  - `kafka-init` 服务存在、复用 Kafka 镜像、挂载脚本、依赖 kafka healthy；
  - `gateway`/`agents`/`replay-service`/`smoke-test` 依赖 `kafka-init` `service_completed_successfully`；
  - KafkaJS producer/consumer `allowAutoTopicCreation: false`；
  - `scripts/kafka-init.sh` 包含 Gateway `TOPICS` 和 Agents `CONSUME_TOPICS` 中的所有 topic。

## 8. 验收标准

1. `docker compose config` 通过。
2. `docker compose up -d` 启动后 `kafka-init` 成功退出（`Completed`）。
3. `docker compose exec kafka kafka-topics --bootstrap-server localhost:9092 --list` 列出全部预期 topic。
4. 删除任意一个 topic 后 `docker compose restart kafka-init` 能恢复它。
5. 将 `kafka-init` 脚本故意写错（如未知 broker），`gateway`/`agents` 不启动。
6. CI smoke job 通过（因为 Gateway 启动前 topic 已就绪）。

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| dev 单节点 Kafka replication-factor 不能 >1 | 脚本读取环境变量 `KAFKA_REPLICATION_FACTOR=${KAFKA_REPLICATION_FACTOR:-1}` |
| topic 清单与代码不同步 | `tests/infra/test_kafka_init.py` 校验 Gateway `TOPICS` 和 Agents `CONSUME_TOPICS` 都被 kafka-init 覆盖 |
| 启动变慢 | kafka-init 通常在数秒内完成；远小于服务健康等待时间 |
| 误删 topic 后数据丢失 | 本方案只能重建空 topic，数据恢复依赖 Replay/Outbox，属 Iteration 3 范畴 |

## 10. 后续扩展

- Helm chart 中通过 `initContainer` 在 gateway/agents pod 启动前运行等价的 topic 创建逻辑，或复用 `kafka-init` Job。
- 将 topic 清单与 JSON Schema 注册联动，实现 schema-version 与 topic 的集中管理。
- Outbox Publisher 加入 Compose 后同样需要 `kafka-init: service_completed_successfully`。
