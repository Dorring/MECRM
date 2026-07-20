# Phase 1: AI Runtime Mode 与 Provider 边界统一

> **分支:** `feat/ma-01-ai-runtime-mode`
> **状态:** 已完成
> **依赖:** Phase 0（`docs/multi-agent/current-state-audit.md`、`docs/multi-agent/implementation-map.md`）

---

## 1. 交付摘要

Phase 1 建立了三种 AI 运行模式（`disabled`、`deterministic`、`live`），统一了全仓 Provider 创建边界，并新增了确定性 Provider 用于 CI 和离线测试。

### 核心交付

| 交付 | 位置 |
|---|---|
| `AIMode` / `AIProvider` / `AgentOrchestrationMode` 枚举 | `agents/src/orchestrator/ai_mode.py` |
| `DeterministicChatProvider` / `DeterministicEmbeddingsProvider` | `agents/src/intelligence/deterministic_provider.py` |
| `DisabledChatProvider` / `DisabledEmbeddingsProvider` | `agents/src/intelligence/providers.py` |
| AI_MODE 门控（`create_chat_model` / `create_embeddings`） | `agents/src/intelligence/providers.py` |
| `provider_health_check()` + `/ready` 端点 | `agents/src/intelligence/providers.py` + `orchestrator/main.py` |
| 配置模型（`AI_MODE`、`AGENT_ORCHESTRATION_MODE`） | `agents/src/orchestrator/config.py`、`.env.example`、`docker-compose.yml` |
| Phase 1 测试（37 pass, 3 skip） | `agents/tests/unit/test_ai_mode.py` |

---

## 2. 三种 AI_MODE

| AI_MODE | 行为 | 网络访问 | Chat Model | Embeddings |
|---|---|---|---|---|
| `disabled` | 不初始化模型；AI 调用抛出 `AIModeDisabledError` | ❌ 否 | `DisabledChatProvider` | `DisabledEmbeddingsProvider` |
| **`deterministic`（默认）** | 本地确定性 Provider；同输入→同输出 | ❌ 否 | `DeterministicChatProvider` | `DeterministicEmbeddingsProvider` |
| `live` | 根据 `AI_PROVIDER` 调用真实模型 | ✅ 是 | `ChatOllama` 或 `ChatOpenAI` | `OllamaEmbeddings` 或 `OpenAIEmbeddings` |

**默认值：`AI_MODE=deterministic`** — 默认启动不连接任何外部模型服务，CI 友好。

---

## 3. Provider 配置矩阵

```
AI_MODE=disabled
  → AI_PROVIDER 被忽略
  → 无模型初始化
  → /ready 返回 status=ready, checks={model: skipped, embeddings: skipped}

AI_MODE=deterministic（默认）
  → AI_PROVIDER 被忽略
  → 本地确定性 Provider
  → /ready 返回 status=ready, checks={chat: available, embeddings: available}

AI_MODE=live
  → 必须配置 AI_PROVIDER
  → AI_PROVIDER=ollama → ChatOllama + OllamaEmbeddings
  → AI_PROVIDER=nvidia_nim → ChatOpenAI + OpenAIEmbeddings（需 API Key）
  → 未配置 Provider → ProviderConfigurationError
  → Provider 不可用 → /ready 返回 degraded
```

---

## 4. Ollama 启动方式

Ollama **不随默认 Compose 启动**。

```bash
# 默认启动（无 Ollama，无模型网络访问）
docker compose up -d

# 需要 Ollama 时显式启用 local-llm profile
docker compose --profile local-llm up -d ollama
```

启动 Ollama 后，需设置 `AI_MODE=live` 才能连接：

```bash
AI_MODE=live
AI_PROVIDER=ollama
```

---

## 5. Deterministic Provider 限制

`DeterministicChatProvider` 和 `DeterministicEmbeddingsProvider`：

- ✅ 同输入→同输出（SHA-256 哈希派生）
- ✅ 固定 Embedding 维度（768）
- ✅ 支持 Fixture 注入（`register_fixture`）
- ✅ 支持故障注入（`error/timeout`、`error/empty`、`error/malformed`、`error/low_confidence`、`error/provider_error`）
- ✅ Provider Metadata 稳定
- ❌ 输出不代表真实语义质量
- ❌ 不应用于声称真实语义检索效果
- ❌ 不替代 PostgreSQL、RLS、OPA、Kafka、Weaviate、审批、审计、业务工具

---

## 6. Health / Readiness 语义

| 端点 | 用途 | Ollama 不可用时 |
|---|---|---|
| `GET /health` | 进程存活 + HTTP 响应 | ✅ 正常（不检查模型） |
| `GET /ready` | 服务就绪（含 AI Provider 健康） | 根据 AI_MODE 返回 degraded 或 ready |

`/ready` 返回示例（deterministic 模式）：

```json
{
  "ai_mode": "deterministic",
  "provider": "none",
  "chat_model": "disabled",
  "embedding_model": "disabled",
  "remote": false,
  "status": "ready",
  "checks": {
    "chat": "available",
    "embeddings": "available",
    "embedding_dimension": 768
  }
}
```

---

## 7. 被统一的 Provider 调用点

替换前有 **6 处** `os.getenv("OLLAMA_*")` 或 `_env("OLLAMA_*")` 绕过 `Settings`：

| 文件 | 修复方式 |
|---|---|
| `support.py` | `os.getenv(...)` → `settings.OLLAMA_EMBED_MODEL`；移除 `import os` |
| `chat_agent.py` | `_env(...)` → `settings.OLLAMA_EMBED_MODEL`；移除 `_env` 函数 |
| `knowledge_agent.py` | 同上 |
| `search_agent.py` | 同上 |
| `publisher.py` | 同上 |
| `audit_indexer.py` | 同上 |
| `compliance_agent.py` | 同上 |
| `main.py` | 同上；移除 `_env` 函数 |

**核心运行路径已无直接 OLLAMA 环境变量读取。** 所有 Chat/Embedding 创建均通过 `create_chat_model()` / `create_embeddings()` 统一工厂。

---

## 8. 尚未统一的历史 Provider 耦合点

| 文件 | 耦合方式 | 状态 |
|---|---|---|
| `voice_ingest.py:54` | `WhisperSTT` 默认 `OLLAMA_URL` 作为 Whisper 端点备选 | **记录**：WHISPER_URL 是正式变量，OLLAMA_URL 作为兼容备选。已添加 TODO |
| `evaluate_retrieval.py:133-134` | 直接使用 `settings.OLLAMA_URL` / `settings.OLLAMA_EMBED_MODEL` | **不迁移**：评测脚本，允许直接访问 Settings |
| `config.py:63-65` | `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` 定义 | **保留**：Settings 是单点定义，合法 |

---

## 9. 兼容策略

| 旧配置 | 行为 |
|---|---|
| `AI_PROVIDER=ollama`（无 `AI_MODE`） | 默认 `AI_MODE=deterministic`，输出兼容警告日志 |
| `AI_PROVIDER=nvidia_nim`（无 `AI_MODE`） | 同上 |
| `create_embeddings(ollama_url=..., embedding_model=...)` | 旧参数名保留为 deprecated alias，新代码建议用 `base_url` / `model` |

---

## 10. 测试覆盖

| 测试类别 | 数量 | 状态 |
|---|---|---|
| AI_MODE 配置解析 | 8 | ✅ |
| Provider Factory 工厂 | 6 | ✅ |
| 网络隔离 | 3 | ⏭ Windows-skipped（Linux CI 通过） |
| Deterministic Chat/Embedding | 12 | ✅ |
| Readiness / Health | 5 | ✅ |
| Disabled Provider 行为 | 3 | ✅ |
| **总计** | **37** | **37 pass / 3 skip / 0 fail** |

**所有测试在无 Ollama、无 NVIDIA API Key 环境下运行。**

---

## 11. 默认启动行为变化

| 项目 | Phase 0（旧） | Phase 1（新） |
|---|---|---|
| 默认 `AI_MODE` | 不存在 | `deterministic` |
| 默认 Chat Model 创建 | `create_chat_model()` → `ChatOllama`（可能连接未启动的 Ollama） | `create_chat_model()` → `DeterministicChatProvider`（不连接网络） |
| 默认 Embeddings 创建 | `create_embeddings()` → `OllamaEmbeddings`（可能连接未启动的 Ollama） | `create_embeddings()` → `DeterministicEmbeddingsProvider`（不连接网络） |
| `/ready` 端点 | 不存在 | 存在，返回 AI Provider 健康状态 |
| Ollama 依赖 | 运行时默认连接 Ollama（若 `AI_PROVIDER=ollama`） | 默认无依赖 |

---

## 12. 本阶段禁止事项验证

- [x] 不实现 Supervisor
- [x] 不实现 AgentRegistry
- [x] 不实现 Planner
- [x] 不实现 Specialist
- [x] 不修改 Kafka Topic 路由
- [x] 不修改 OPA 权限语义
- [x] 不修改 RLS
- [x] 不修改审批状态机
- [x] 不新增业务数据库表
- [x] 不默认启动 Ollama
- [x] 不保存 Chain-of-thought
- [x] 不大规模重构与 Provider 无关的代码
