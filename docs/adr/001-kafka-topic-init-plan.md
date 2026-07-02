# Kafka Init – Implementation Plan

> 目标分支：`codex/kafka-init`（从 `main@d6035d7` 切出）
> 状态：Implemented
> 关联：ADR-001

## 变更文件

1. `docker-compose.yml`
   - 关闭 `KAFKA_AUTO_CREATE_TOPICS_ENABLE`
   - 新增 `kafka-init` 服务（复用 `confluentinc/cp-kafka:7.5.0`）
   - 修改 `gateway`/`agents`/`replay-service`/`smoke-test` 的 `depends_on`
2. `scripts/kafka-init.sh` — 完整 topic 清单、leader 就绪检查、失败快速退出
3. `scripts/kafka-init.ps1` — PowerShell 等价脚本
4. `tests/infra/test_kafka_init.py` — 静态回归测试（auto-create 关闭、service 存在、下游依赖、KafkaJS 配置、topic 覆盖）
5. `docs/adr/001-kafka-topic-init.md` — ADR 刷新为 Implemented
6. `.env.example` — 新增 `KAFKA_REPLICATION_FACTOR`、`KAFKA_DEFAULT_RETENTION_MS` 等可选变量
7. `gateway/src/services/kafka.ts` — producer/consumer 设置 `allowAutoTopicCreation: false`

## Compose 变更摘要

```yaml
  kafka:
    environment:
      # ADR-001: explicit topic creation; do not rely on auto-create.
      - KAFKA_AUTO_CREATE_TOPICS_ENABLE=false

  kafka-init:
    image: confluentinc/cp-kafka:7.5.0
    entrypoint: ["bash", "/scripts/kafka-init.sh"]
    environment:
      - KAFKA_BROKERS=kafka:9092
      - KAFKA_REPLICATION_FACTOR=${KAFKA_REPLICATION_FACTOR:-1}
      - KAFKA_DEFAULT_RETENTION_MS=${KAFKA_DEFAULT_RETENTION_MS:-604800000}
      - KAFKA_AUDIT_RETENTION_MS=${KAFKA_AUDIT_RETENTION_MS:-2592000000}
      - KAFKA_DLQ_RETENTION_MS=${KAFKA_DLQ_RETENTION_MS:-1209600000}
      - KAFKA_LEADER_TIMEOUT=${KAFKA_LEADER_TIMEOUT:-120}
      - KAFKA_LEADER_INTERVAL=${KAFKA_LEADER_INTERVAL:-2}
    volumes:
      - ./scripts/kafka-init.sh:/scripts/kafka-init.sh:ro
    depends_on:
      kafka:
        condition: service_healthy
    networks:
      - crm-network

  gateway:
    depends_on:
      kafka-init:
        condition: service_completed_successfully
      # ... keep postgres/redis/opa

  agents:
    depends_on:
      kafka-init:
        condition: service_completed_successfully
      # ... keep postgres/redis/weaviate/opa

  replay-service:
    depends_on:
      kafka-init:
        condition: service_completed_successfully
      # ... keep postgres

  smoke-test:
    depends_on:
      kafka-init:
        condition: service_completed_successfully
      # ... keep gateway/postgres/redis
```

## 验证步骤

1. `python -m pytest tests/infra/test_kafka_init.py -v`（静态测试）
2. `docker compose config`（Compose 配置校验）
3. `docker compose up -d`
4. `docker compose ps kafka-init` 状态为 `Exited (0)`
5. `docker compose exec kafka kafka-topics --bootstrap-server localhost:9092 --list` 列出全部 topic
6. 删除一个 topic 后 `docker compose restart kafka-init`，确认 topic 恢复
7. 把 `kafka-init.sh` 改成错误 broker，确认 `gateway`/`agents` 不启动
8. CI smoke job 全绿

> topic 删除恢复测试只能在全新、可丢弃的本地 Kafka 数据卷中进行，避免删除已有业务数据。

## 与 Iteration 3 的关系

kafka-init 不是 Outbox 的一部分，但为 Outbox 提供了可预测的 Kafka 拓扑。Outbox Publisher 和消费者可以依赖显式 topic 配置，而不是 auto-create 的隐式行为。

## 提交信息建议

```
feat(kafka): explicit topic initialization service (ADR-001)

- Add kafka-init service that creates all CRM topics before gateway/agents start.
- Disable KAFKA_AUTO_CREATE_TOPICS_ENABLE and KafkaJS allowAutoTopicCreation.
- Gateway/agents/replay-service/smoke-test wait for kafka-init service_completed_successfully.
- Centralize topic list, partitions, retention in scripts/kafka-init.sh.
- Add PowerShell equivalent and infra regression tests covering topic coverage.
```
